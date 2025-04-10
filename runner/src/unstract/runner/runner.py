import ast
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from dotenv import load_dotenv
from flask import Flask
from unstract.runner.clients.helper import ContainerClientHelper
from unstract.runner.clients.interface import (
    ContainerClientInterface,
    ContainerInterface,
)
from unstract.runner.constants import Env, LogLevel, LogType, ToolKey
from unstract.runner.exception import ToolRunException

from unstract.core.constants import LogFieldName
from unstract.core.pubsub_helper import LogPublisher

load_dotenv()
# Loads the container clinet class.
client_class = ContainerClientHelper.get_container_client()


class UnstractRunner:
    def __init__(self, image_name: str, image_tag: str, app: Flask) -> None:
        self.image_name = image_name
        # If no image_tag is provided will assume the `latest` tag
        self.image_tag = image_tag or "latest"
        self.logger = app.logger
        self.client: ContainerClientInterface = client_class(
            self.image_name, self.image_tag, self.logger
        )

    # Function to stream logs
    def stream_logs(
        self,
        container: ContainerInterface,
        tool_instance_id: str,
        execution_id: str,
        organization_id: str,
        file_execution_id: str,
        channel: Optional[str] = None,
    ) -> None:
        for line in container.logs(follow=True):
            log_message = line
            self.logger.debug(f"[{container.name}] - {log_message}")
            self.process_log_message(
                log_message=log_message,
                tool_instance_id=tool_instance_id,
                channel=channel,
                execution_id=execution_id,
                organization_id=organization_id,
                file_execution_id=file_execution_id,
            )

    def get_valid_log_message(self, log_message: str) -> Optional[dict[str, Any]]:
        """Get a valid log message from the log message.

        Args:
            log_message (str): str

        Returns:
            Optional[dict[str, Any]]: json message
        """
        try:
            log_dict = json.loads(log_message)
            if isinstance(log_dict, dict):
                return log_dict
        except json.JSONDecodeError:
            return None

    def process_log_message(
        self,
        log_message: str,
        tool_instance_id: str,
        execution_id: str,
        organization_id: str,
        file_execution_id: str,
        channel: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        log_dict = self.get_valid_log_message(log_message)
        if not log_dict:
            return None
        log_type = log_dict.get("type")
        log_level = log_dict.get("level")
        if log_type == LogType.LOG and log_level == LogLevel.ERROR:
            raise ToolRunException(log_dict.get("log"))
        if not self.is_valid_log_type(log_type):
            self.logger.warning(
                f"Received invalid logType: {log_type} with log message: {log_dict}"
            )
            return None
        if log_type == LogType.RESULT:
            return log_dict
        if log_type == LogType.UPDATE:
            log_dict["component"] = tool_instance_id
        if channel:
            log_dict[LogFieldName.EXECUTION_ID] = execution_id
            log_dict[LogFieldName.ORGANIZATION_ID] = organization_id
            log_dict[LogFieldName.TIMESTAMP] = self._get_log_timestamp(log_dict)
            log_dict[LogFieldName.FILE_EXECUTION_ID] = file_execution_id

            # Publish to channel of socket io
            LogPublisher.publish(channel, log_dict)
        return None

    def is_valid_log_type(self, log_type: Optional[str]) -> bool:
        if log_type in {
            LogType.LOG,
            LogType.UPDATE,
            LogType.COST,
            LogType.RESULT,
            LogType.SINGLE_STEP,
        }:
            return True
        return False

    def _get_log_timestamp(self, log_dict: dict[str, Any]) -> float:
        """Obtains the timestamp from the log dictionary.

        Checks if the log dictionary has an `emitted_at` key and returns the
        corresponding timestamp. If the key is not present, returns the current
        timestamp.

        Args:
            log_dict (dict[str, Any]): Log message from tool

        Returns:
            float: Timestamp of the log message
        """
        # Use current timestamp if emitted_at is not present
        if "emitted_at" not in log_dict:
            return datetime.now(timezone.utc).timestamp()

        emitted_at = log_dict["emitted_at"]
        if isinstance(emitted_at, str):
            # Convert ISO format string to UNIX timestamp
            return datetime.fromisoformat(emitted_at).timestamp()
        elif isinstance(emitted_at, (int, float)):
            # Already a UNIX timestamp
            return float(emitted_at)

    def _parse_additional_envs(self) -> dict[str, Any]:
        """Parse TOOL_ADDITIONAL_ENVS environment variable to get additional
        environment variables.
        Also propagates OpenTelemetry trace context if available.

        Returns:
            dict: Dictionary containing parsed environment variables
                  or empty dict if none found.
        """
        additional_envs: dict[str, Any] = {}

        # Get additional envs from environment variable
        tool_additional_envs = os.getenv("TOOL_ADDITIONAL_ENVS")
        if not tool_additional_envs:
            return additional_envs
        try:
            additional_envs = json.loads(tool_additional_envs)
            self.logger.info(
                f"Parsed additional environment variables: {additional_envs}"
            )
        except json.JSONDecodeError as e:
            self.logger.warning(f"Failed to parse TOOL_ADDITIONAL_ENVS: {e}")

        # Propagate OpenTelemetry trace context if available
        # This is required only if additional envs are present
        try:
            from opentelemetry import trace

            current_span = trace.get_current_span()
            if current_span.is_recording():
                span_context = current_span.get_span_context()
                if span_context.is_valid:
                    # Add trace context to environment variables
                    additional_envs.update(
                        {
                            "OTEL_PROPAGATORS": "tracecontext",
                            "OTEL_TRACE_ID": f"{span_context.trace_id:032x}",
                            "OTEL_SPAN_ID": f"{span_context.span_id:016x}",
                            "OTEL_TRACE_FLAGS": f"{span_context.trace_flags:02x}",
                        }
                    )
                    self.logger.debug(
                        f"Propagating trace context: {span_context.trace_id:032x}"
                    )
        except Exception as e:
            # OpenTelemetry not available or error occurred, skip trace propagation
            self.logger.debug(f"Skipping trace propagation: {e}")

        return additional_envs

    def run_command(self, command: str) -> Optional[Any]:
        """Runs any given command on the container.

        Args:
            command (str): Command to be executed.

        Returns:
            Optional[Any]: Response from container or None if error occures.
        """
        command = command.upper()

        # Get additional environment variables to pass to the container
        additional_env = self._parse_additional_envs()

        container_config = self.client.get_container_run_config(
            command=["--command", command],
            file_execution_id="",
            auto_remove=True,
            envs=additional_env,
        )
        container = None

        # Run the Docker container
        try:
            container: ContainerInterface = self.client.run_container(container_config)
            for text in container.logs(follow=True):
                self.logger.info(f"[{container.name}] - {text}")
                if f'"type": "{command}"' in text:
                    return json.loads(text)
        except Exception as e:
            self.logger.error(
                f"Failed to run docker container: {e}", stack_info=True, exc_info=True
            )
        if container:
            container.cleanup()
        return None

    def run_container(
        self,
        organization_id: str,
        workflow_id: str,
        execution_id: str,
        file_execution_id: str,
        settings: dict[str, Any],
        envs: dict[str, Any],
        messaging_channel: Optional[str] = None,
        container_name: Optional[str] = None,
    ) -> Optional[Any]:
        """RUN container With RUN Command.

        Args:
            workflow_id (str): projectId
            params (dict[str, Any]): params to run the tool
            settings (dict[str, Any]): Tool settings
            envs (dict[str, Any]): Tool env
            messaging_channel (Optional[str], optional): socket io channel

        Returns:
            Optional[Any]: _description_
        """

        envs[Env.EXECUTION_DATA_DIR] = os.path.join(
            os.getenv(Env.WORKFLOW_EXECUTION_DIR_PREFIX, ""),
            organization_id,
            workflow_id,
            execution_id,
        )
        envs[Env.WORKFLOW_EXECUTION_FILE_STORAGE_CREDENTIALS] = os.getenv(
            Env.WORKFLOW_EXECUTION_FILE_STORAGE_CREDENTIALS, "{}"
        )

        # Get additional environment variables to pass to the container
        additional_env = self._parse_additional_envs()

        container_config = self.client.get_container_run_config(
            command=[
                "--command",
                "RUN",
                "--settings",
                json.dumps(settings),
                "--log-level",
                "DEBUG",
            ],
            file_execution_id=file_execution_id,
            container_name=container_name,
            envs={**envs, **additional_env},
        )
        # Add labels to container for logging with Loki.
        # This only required for observability.
        try:
            labels = ast.literal_eval(os.getenv(Env.TOOL_CONTAINER_LABELS, "[]"))
            container_config["labels"] = labels
        except Exception as e:
            self.logger.info(f"Invalid labels for logging: {e}")

        # Run the Docker container
        container = None
        result = {"type": "RESULT", "result": None}
        try:
            self.logger.info(
                f"Execution ID: {execution_id}, running docker "
                f"container: {container_name}"
            )
            container: ContainerInterface = self.client.run_container(container_config)
            tool_instance_id = str(settings.get(ToolKey.TOOL_INSTANCE_ID))
            # Stream logs
            self.stream_logs(
                container=container,
                tool_instance_id=tool_instance_id,
                channel=messaging_channel,
                execution_id=execution_id,
                organization_id=organization_id,
                file_execution_id=file_execution_id,
            )
            self.logger.info(
                f"Execution ID: {execution_id}, docker "
                f"container: {container_name} ran successfully"
            )
        except ToolRunException as te:
            self.logger.error(
                "Error while running docker container"
                f" {container_config.get('name')}: {te}",
                stack_info=True,
                exc_info=True,
            )
            result = {"type": "RESULT", "result": None, "error": str(te.message)}
        except Exception as e:
            self.logger.error(
                f"Failed to run docker container: {e}", stack_info=True, exc_info=True
            )
            result = {"type": "RESULT", "result": None, "error": str(e)}
        if container:
            container.cleanup()
        return result

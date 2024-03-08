#  Copyright (c) ZenML GmbH 2024. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at:
#
#       https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
#  or implied. See the License for the specific language governing
#  permissions and limitations under the License.
"""Implementation of the Hugging Face Model Deployer."""

import os
from typing import ClassVar, Dict, Optional, Type, cast
from uuid import UUID

from zenml.analytics.enums import AnalyticsEvent
from zenml.analytics.utils import track_handler
from zenml.artifacts.utils import log_artifact_metadata, save_artifact
from zenml.client import Client
from zenml.integrations.huggingface import HUGGINGFACE_SERVICE_ARTIFACT
from zenml.integrations.huggingface.flavors.huggingface_model_deployer_flavor import (
    HuggingFaceModelDeployerConfig,
    HuggingFaceModelDeployerFlavor,
)
from zenml.integrations.huggingface.services.huggingface_deployment import (
    HuggingFaceDeploymentService,
    HuggingFaceServiceConfig,
)
from zenml.logger import get_logger
from zenml.model_deployers import BaseModelDeployer
from zenml.model_deployers.base_model_deployer import (
    DEFAULT_DEPLOYMENT_START_STOP_TIMEOUT,
    BaseModelDeployerFlavor,
)
from zenml.services import BaseService, ServiceConfig

logger = get_logger(__name__)

ZENM_ENDPOINT_PREFIX: str = "zenml-"
UUID_SLICE_LENGTH: int = 8


class HuggingFaceModelDeployer(BaseModelDeployer):
    """Hugging Face endpoint model deployer."""

    NAME: ClassVar[str] = "HuggingFace"
    FLAVOR: ClassVar[Type[BaseModelDeployerFlavor]] = (
        HuggingFaceModelDeployerFlavor
    )

    @property
    def config(self) -> HuggingFaceModelDeployerConfig:
        """Config class for the Hugging Face Model deployer settings class.

        Returns:
            The configuration.
        """
        return cast(HuggingFaceModelDeployerConfig, self._config)

    def prepare_environment_variable(self, set: bool = True) -> None:
        """Set up Environment variables that are required for the authenticating with Hugging Face.

        Args:
            set: Whether to set the environment variables or not.

        Raises:
            ValueError: If no service connector is found.
        """
        if set:
            os.environ["HF_TOKEN"] = self.config.token
            os.environ["HF_NAMESPACE"] = self.config.namespace
        else:
            os.environ.pop("HF_TOKEN", None)
            os.environ.pop("HF_NAMESPACE", None)

    def modify_endpoint_name(
        self, endpoint_name: str, artifact_version: str
    ) -> str:
        """Modify endpoint name by adding suffix and prefix.

        It adds a prefix "zenml-" if not present and a suffix
        of first 8 characters of uuid.

        Args:
            endpoint_name : Name of the endpoint
            artifact_version: Name of the artifact version

        Returns:
            Modified endpoint name with added prefix and suffix
        """
        # Add prefix if it does not start with ZENM_ENDPOINT_PREFIX
        if not endpoint_name.startswith(ZENM_ENDPOINT_PREFIX):
            endpoint_name = ZENM_ENDPOINT_PREFIX + endpoint_name

        endpoint_name += artifact_version
        return endpoint_name

    def _create_new_service(
        self, id: UUID, timeout: int, config: HuggingFaceServiceConfig
    ) -> HuggingFaceDeploymentService:
        """Creates a new Hugging FaceDeploymentService.

        Args:
            id: the UUID of the model to be deployed with Hugging Face model deployer.
            timeout: the timeout in seconds to wait for the Hugging Face inference endpoint
                to be provisioned and successfully started or updated.
            config: the configuration of the model to be deployed with Hugging Face model deployer.

        Returns:
            The HuggingFaceServiceConfig object that can be used to interact
            with the Hugging Face inference endpoint.
        """
        # create a new service for the new model
        service = HuggingFaceDeploymentService(uuid=id, config=config)

        # Use first 8 characters of UUID as artifact version
        artifact_version = str(id)[:UUID_SLICE_LENGTH]
        # Add same 8 characters as suffix to endpoint name
        service.config.endpoint_name = self.modify_endpoint_name(
            service.config.endpoint_name, artifact_version
        )

        logger.info(
            f"Creating an artifact {HUGGINGFACE_SERVICE_ARTIFACT} with service instance attached as metadata."
            " If there's an active pipeline and/or model this artifact will be associated with it."
        )

        save_artifact(
            service,
            HUGGINGFACE_SERVICE_ARTIFACT,
            version=artifact_version,
            is_deployment_artifact=True,
        )

        # Convert UUID object to be json serializable
        service_metadata = service.dict()
        service_metadata["uuid"] = str(service_metadata["uuid"])
        log_artifact_metadata(
            artifact_name=HUGGINGFACE_SERVICE_ARTIFACT,
            artifact_version=artifact_version,
            metadata={HUGGINGFACE_SERVICE_ARTIFACT: service_metadata},
        )

        service.start(timeout=timeout)
        return service

    def _clean_up_existing_service(
        self,
        timeout: int,
        force: bool,
        existing_service: HuggingFaceDeploymentService,
    ) -> None:
        """Stop existing services.

        Args:
            timeout: the timeout in seconds to wait for the Hugging Face
                deployment to be stopped.
            force: if True, force the service to stop
            existing_service: Existing Hugging Face deployment service
        """
        # stop the older service
        self.prepare_environment_variable(set=True)
        existing_service.stop(timeout=timeout, force=force)
        self.prepare_environment_variable(set=False)

    def perform_deploy_model(
        self,
        id: UUID,
        config: ServiceConfig,
        timeout: int = DEFAULT_DEPLOYMENT_START_STOP_TIMEOUT,
    ) -> BaseService:
        """Create a new Hugging Face deployment service or update an existing one.

        This should serve the supplied model and deployment configuration.

        Args:
            id: the UUID of the model to be deployed with Hugging Face.
            config: the configuration of the model to be deployed with Hugging Face.
                Core
            replace: set this flag to True to find and update an equivalent
                Hugging Face deployment server with the new model instead of
                starting a new deployment server.
            timeout: the timeout in seconds to wait for the Hugging Face endpoint
                to be provisioned and successfully started or updated. If set
                to 0, the method will return immediately after the Hugging Face
                server is provisioned, without waiting for it to fully start.

        Returns:
            The ZenML Hugging Face deployment service object that can be used to
            interact with the remote Hugging Face inference endpoint server.
        """
        with track_handler(AnalyticsEvent.MODEL_DEPLOYED) as analytics_handler:
            config = cast(HuggingFaceServiceConfig, config)
            # create a new HuggingFaceDeploymentService instance
            self.prepare_environment_variable(set=True)
            service = self._create_new_service(
                id=id, timeout=timeout, config=config
            )
            self.prepare_environment_variable(set=False)
            logger.info(
                f"Creating a new Hugging Face inference endpoint service: {service}"
            )
            # Add telemetry with metadata that gets the stack metadata and
            # differentiates between pure model and custom code deployments
            stack = Client().active_stack
            stack_metadata = {
                component_type.value: component.flavor
                for component_type, component in stack.components.items()
            }
            analytics_handler.metadata = {
                "store_type": Client().zen_store.type.value,
                **stack_metadata,
            }

        return service

    def perform_stop_model(
        self,
        service: BaseService,
        timeout: int = DEFAULT_DEPLOYMENT_START_STOP_TIMEOUT,
        force: bool = False,
    ) -> BaseService:
        """Method to stop a model server.

        Args:
            service: The service to stop.
            timeout: Timeout in seconds to wait for the service to stop.
            force: If True, force the service to stop.

        Returns:
            The stopped service.
        """
        self.prepare_environment_variable(set=True)
        service.stop(timeout=timeout, force=force)
        self.prepare_environment_variable(set=False)
        return service

    def perform_start_model(
        self,
        service: BaseService,
        timeout: int = DEFAULT_DEPLOYMENT_START_STOP_TIMEOUT,
    ) -> BaseService:
        """Method to start a model server.

        Args:
            service: The service to start.
            timeout: Timeout in seconds to wait for the service to start.

        Returns:
            The started service.
        """
        self.prepare_environment_variable(set=True)
        service.start(timeout=timeout)
        self.prepare_environment_variable(set=False)
        return service

    def perform_delete_model(
        self,
        service: BaseService,
        timeout: int = DEFAULT_DEPLOYMENT_START_STOP_TIMEOUT,
        force: bool = False,
    ) -> None:
        """Method to delete all configuration of a model server.

        Args:
            service: The service to delete.
            timeout: Timeout in seconds to wait for the service to stop.
            force: If True, force the service to stop.
        """
        service = cast(HuggingFaceDeploymentService, service)
        self.prepare_environment_variable(set=True)
        self._clean_up_existing_service(
            existing_service=service, timeout=timeout, force=force
        )
        self.prepare_environment_variable(set=False)

    @staticmethod
    def get_model_server_info(  # type: ignore[override]
        service_instance: "HuggingFaceDeploymentService",
    ) -> Dict[str, Optional[str]]:
        """Return implementation specific information that might be relevant to the user.

        Args:
            service_instance: Instance of a HuggingFaceDeploymentService

        Returns:
            Model server information.
        """
        return {
            "PREDICTION_URL": service_instance.get_the_prediction_url(),
            "HEALTH_CHECK_URL": service_instance.get_the_healthcheck_url(),
        }

import os
import re
import sys
from typing import Optional

from colorama import Fore, Style
from google.auth import default, impersonated_credentials, load_credentials_from_dict
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient import discovery
from googleapiclient.errors import HttpError

from prowler.config.config import (
    default_config_file_path,
    get_default_mute_file_path,
    load_and_validate_config_file,
)
from prowler.lib.logger import logger
from prowler.lib.utils.utils import print_boxes
from prowler.providers.common.models import Audit_Metadata, Connection
from prowler.providers.common.provider import Provider
from prowler.providers.gcp.exceptions.exceptions import (
    GCPCloudResourceManagerAPINotUsedError,
    GCPGetProjectError,
    GCPHTTPError,
    GCPInvalidProviderIdError,
    GCPLoadCredentialsFromDictError,
    GCPNoAccesibleProjectsError,
    GCPSetUpSessionError,
    GCPStaticCredentialsError,
    GCPTestConnectionError,
)
from prowler.providers.gcp.lib.mutelist.mutelist import GCPMutelist
from prowler.providers.gcp.models import GCPIdentityInfo, GCPOrganization, GCPProject


class GcpProvider(Provider):
    _type: str = "gcp"
    _session: Credentials
    _project_ids: list
    _excluded_project_ids: list
    _identity: GCPIdentityInfo
    _audit_config: dict
    _mutelist: GCPMutelist
    # TODO: this is not optional, enforce for all providers
    audit_metadata: Audit_Metadata

    def __init__(
        self,
        project_ids: list = None,
        excluded_project_ids: list = None,
        credentials_file: str = None,
        impersonate_service_account: str = None,
        list_project_ids: bool = False,
        config_path: str = None,
        config_content: dict = None,
        fixer_config: dict = {},
        mutelist_path: str = None,
        mutelist_content: dict = None,
        client_id: str = None,
        client_secret: str = None,
        refresh_token: str = None,
    ):
        """
        GCP Provider constructor

        Args:
            project_ids: list
            excluded_project_ids: list
            credentials_file: str
            impersonate_service_account: str
            list_project_ids: bool
            config_path: str
            config_content: dict
            fixer_config: dict
            mutelist_path: str
            mutelist_content: dict
            client_id: str
            client_secret: str
            refresh_token: str
        """
        logger.info("Instantiating GCP Provider ...")
        self._impersonated_service_account = impersonate_service_account
        # Set the GCP credentials using the provided client_id, client_secret and refresh_token
        gcp_credentials = None
        if any([client_id, client_secret, refresh_token]):
            gcp_credentials = self.validate_static_arguments(
                client_id, client_secret, refresh_token
            )

        self._session, self._default_project_id = self.setup_session(
            credentials_file, self._impersonated_service_account, gcp_credentials
        )

        self._project_ids = []
        self._projects = {}
        self._excluded_project_ids = []
        accessible_projects = self.get_projects(self._session)
        if not accessible_projects:
            logger.critical("No Project IDs can be accessed via Google Credentials.")
            raise GCPNoAccesibleProjectsError(
                file=__file__,
                message="No Project IDs can be accessed via Google Credentials.",
            )

        if project_ids:
            for input_project in project_ids:
                for accessible_project in accessible_projects:
                    if self.is_project_matching(input_project, accessible_project):
                        self._projects[accessible_project] = accessible_projects[
                            accessible_project
                        ]
                        self._project_ids.append(
                            accessible_projects[accessible_project].id
                        )
        else:
            # If not projects were input, all accessible projects are scanned by default
            for project_id, project in accessible_projects.items():
                self._projects[project_id] = project
                self._project_ids.append(project_id)

        # Remove excluded projects if any input
        if excluded_project_ids:
            for excluded_project in excluded_project_ids:
                for project_id in self._project_ids:
                    if self.is_project_matching(excluded_project, project_id):
                        self._excluded_project_ids.append(project_id)
            for project_id in self._excluded_project_ids:
                self._projects.pop(project_id)
                self._project_ids.remove(project_id)

        if not self._projects:
            logger.critical(
                "No Input Project IDs can be accessed via Google Credentials."
            )
            raise GCPNoAccesibleProjectsError(
                file=__file__,
                message="No Input Project IDs can be accessed via Google Credentials.",
            )

        if list_project_ids:
            print(
                f"{Fore.YELLOW}Available GCP Project IDs{Style.RESET_ALL}:\n{' '.join(self._project_ids)}\n"
            )
            sys.exit(0)

        # Update organizations info
        self.update_projects_with_organizations()

        self._identity = GCPIdentityInfo(
            profile=getattr(self.session, "_service_account_email", "default"),
        )

        # TODO: move this to the providers, pending for AWS, GCP, AZURE and K8s
        # Audit Config
        if config_content:
            self._audit_config = config_content
        else:
            if not config_path:
                config_path = default_config_file_path
            self._audit_config = load_and_validate_config_file(self._type, config_path)

        # Fixer Config
        self._fixer_config = fixer_config

        # Mutelist
        if mutelist_content:
            self._mutelist = GCPMutelist(
                mutelist_content=mutelist_content,
            )
        else:
            if not mutelist_path:
                mutelist_path = get_default_mute_file_path(self.type)
            self._mutelist = GCPMutelist(
                mutelist_path=mutelist_path,
            )

        Provider.set_global_provider(self)

    @property
    def identity(self):
        return self._identity

    @property
    def type(self):
        return self._type

    @property
    def session(self):
        return self._session

    @property
    def projects(self):
        return self._projects

    @property
    def default_project_id(self):
        return self._default_project_id

    @property
    def impersonated_service_account(self):
        return self._impersonated_service_account

    @property
    def project_ids(self):
        return self._project_ids

    @property
    def excluded_project_ids(self):
        return self._excluded_project_ids

    @property
    def audit_config(self):
        return self._audit_config

    @property
    def fixer_config(self):
        return self._fixer_config

    @property
    def mutelist(self) -> GCPMutelist:
        """
        mutelist method returns the provider's mutelist.
        """
        return self._mutelist

    @staticmethod
    def setup_session(
        credentials_file: str, service_account: str, gcp_credentials: dict = None
    ) -> tuple:
        """
        Setup the GCP session with the provided credentials file or service account to impersonate
        Args:
            credentials_file: str
            service_account: str
        Returns:
            Credentials object and default project ID
        """
        try:
            scopes = ["https://www.googleapis.com/auth/cloud-platform"]

            if gcp_credentials:
                logger.info("Using GCP static credentials")
                logger.info("GCP provider: Setting credentials from dict...")
                try:
                    credentials, default_project_id = load_credentials_from_dict(
                        info=gcp_credentials
                    )
                    return credentials, default_project_id
                except Exception as error:
                    logger.critical(
                        f"{error.__class__.__name__}[{error.__traceback__.tb_lineno}]: {error}"
                    )
                    raise GCPLoadCredentialsFromDictError(
                        file=__file__, original_exception=error
                    )

            if credentials_file:
                logger.info(f"Using credentials file: {credentials_file}")
                logger.info(
                    "GCP provider: Setting GOOGLE_APPLICATION_CREDENTIALS environment variable..."
                )
                client_secrets_path = os.path.abspath(credentials_file)
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = client_secrets_path

            # Get default credentials
            credentials, default_project_id = default(scopes=scopes)

            # Refresh the credentials to ensure they are valid
            credentials.refresh(Request())

            logger.info(f"Initial credentials: {credentials}")

            if service_account:
                # Create the impersonated credentials
                credentials = impersonated_credentials.Credentials(
                    source_credentials=credentials,
                    target_principal=service_account,
                    target_scopes=scopes,
                )
                logger.info(f"Impersonated credentials: {credentials}")

            return credentials, default_project_id
        except Exception as error:
            logger.critical(
                f"{error.__class__.__name__}[{error.__traceback__.tb_lineno}]: {error}"
            )
            raise GCPSetUpSessionError(file=__file__, original_exception=error)

    @staticmethod
    def test_connection(
        credentials_file: str = None,
        service_account: str = None,
        raise_on_exception: bool = True,
        client_id: str = None,
        client_secret: str = None,
        refresh_token: str = None,
        provider_id: Optional[str] = None,
    ) -> Connection:
        """
        Test the connection to GCP with the provided credentials file or service account to impersonate.
        If the connection is successful, return a Connection object with is_connected set to True. If the connection fails, return a Connection object with error set to the exception.
        Raise an exception if raise_on_exception is set to True.
        If the Cloud Resource Manager API has not been used before or it is disabled, log a critical message and return a Connection object with error set to the exception.
        Args:
            credentials_file: str
            service_account: str
            raise_on_exception: bool
            client_id: str
            client_secret: str
            refresh_token: str
            provider_id: Optional[str] -> The provider ID, for GCP it is the project ID
        Returns:
            Connection object with is_connected set to True if the connection is successful, or error set to the exception if the connection fails
        """
        try:
            # Set the GCP credentials using the provided client_id, client_secret and refresh_token
            gcp_credentials = None
            if any([client_id, client_secret, refresh_token]):
                gcp_credentials = GcpProvider.validate_static_arguments(
                    client_id, client_secret, refresh_token
                )

            session, project_id = GcpProvider.setup_session(
                credentials_file, service_account, gcp_credentials
            )
            if provider_id and project_id != provider_id:
                # Logic to check if the provider ID matches the project ID
                GcpProvider.validate_project_id(
                    provider_id=provider_id, credentials=session
                )

            service = discovery.build("cloudresourcemanager", "v1", credentials=session)
            request = service.projects().list()
            request.execute()
            return Connection(is_connected=True)

        # Errors from setup_session
        except GCPLoadCredentialsFromDictError as load_credentials_error:
            logger.critical(
                f"{load_credentials_error.__class__.__name__}[{load_credentials_error.__traceback__.tb_lineno}]: {load_credentials_error}"
            )
            if raise_on_exception:
                raise load_credentials_error
            return Connection(error=load_credentials_error)
        except GCPSetUpSessionError as setup_session_error:
            logger.critical(
                f"{setup_session_error.__class__.__name__}[{setup_session_error.__traceback__.tb_lineno}]: {setup_session_error}"
            )
            if raise_on_exception:
                raise setup_session_error
            return Connection(error=setup_session_error)
        except HttpError as http_error:
            if "Cloud Resource Manager API has not been used" in str(http_error):
                logger.critical(
                    "Cloud Resource Manager API has not been used before or it is disabled. Enable it by visiting https://console.developers.google.com/apis/api/cloudresourcemanager.googleapis.com/ then retry."
                )
                if raise_on_exception:
                    raise GCPCloudResourceManagerAPINotUsedError(
                        file=__file__, original_exception=http_error
                    )
            else:
                logger.critical(
                    f"{http_error.__class__.__name__}[{http_error.__traceback__.tb_lineno}]: {http_error}"
                )
            if raise_on_exception:
                raise http_error
            return Connection(error=http_error)
        # Exceptions from validating Provider ID
        except GCPInvalidProviderIdError as not_valid_provider_id_error:
            logger.critical(
                f"{not_valid_provider_id_error.__class__.__name__}[{not_valid_provider_id_error.__traceback__.tb_lineno}]: {not_valid_provider_id_error}"
            )
            if raise_on_exception:
                raise not_valid_provider_id_error
            return Connection(error=not_valid_provider_id_error)
        except Exception as error:
            logger.critical(
                f"{error.__class__.__name__}[{error.__traceback__.tb_lineno}]: {error}"
            )
            if raise_on_exception:
                raise GCPTestConnectionError(file=__file__, original_exception=error)
            return Connection(error=error)

    def print_credentials(self):
        # TODO: Beautify audited profile, set "default" if there is no profile set
        # TODO: improve print_credentials with more data like name, number, organization
        report_lines = [
            f"GCP Account: {Fore.YELLOW}{self.identity.profile}{Style.RESET_ALL}",
            f"GCP Project IDs: {Fore.YELLOW}{', '.join(self.project_ids)}{Style.RESET_ALL}",
        ]
        if self.identity.profile:
            report_lines.append(
                f"Profile: {Fore.YELLOW}{self.identity.profile}{Style.RESET_ALL}"
            )
        if self.impersonated_service_account:
            report_lines.append(
                f"Impersonated Service Account: {Fore.YELLOW}{self.impersonated_service_account}{Style.RESET_ALL}"
            )
        if self.excluded_project_ids:
            report_lines.append(
                f"Excluded GCP Project IDs: {Fore.YELLOW}{', '.join(self.excluded_project_ids)}{Style.RESET_ALL}"
            )
        report_title = (
            f"{Style.BRIGHT}Using the GCP credentials below:{Style.RESET_ALL}"
        )
        print_boxes(report_lines, report_title)

    @staticmethod
    def get_projects(credentials) -> dict[str, GCPProject]:
        try:
            projects = {}

            service = discovery.build(
                "cloudresourcemanager", "v1", credentials=credentials
            )

            request = service.projects().list()

            while request is not None:
                response = request.execute()

                for project in response.get("projects", []):
                    labels = {}
                    for key, value in project.get("labels", {}).items():
                        labels[key] = value

                    project_id = project["projectId"]
                    gcp_project = GCPProject(
                        number=project["projectNumber"],
                        id=project_id,
                        name=project.get("name", project_id),
                        lifecycle_state=project["lifecycleState"],
                        labels=labels,
                    )

                    if (
                        "parent" in project
                        and "type" in project["parent"]
                        and project["parent"]["type"] == "organization"
                    ):
                        organization_id = project["parent"]["id"]
                        gcp_project.organization = GCPOrganization(
                            id=organization_id, name=f"organizations/{organization_id}"
                        )

                    projects[project_id] = gcp_project
                request = service.projects().list_next(
                    previous_request=request, previous_response=response
                )

        except HttpError as http_error:
            if "Cloud Resource Manager API has not been used" in str(http_error):
                logger.critical(
                    "Cloud Resource Manager API has not been used before or it is disabled. Enable it by visiting https://console.developers.google.com/apis/api/cloudresourcemanager.googleapis.com/ then retry."
                )
                raise GCPCloudResourceManagerAPINotUsedError(
                    file=__file__, original_exception=http_error
                )
            else:
                logger.error(
                    f"{http_error.__class__.__name__}[{http_error.__traceback__.tb_lineno}]: {http_error}"
                )
                raise GCPHTTPError(file=__file__, original_exception=http_error)
        except Exception as error:
            logger.critical(
                f"{error.__class__.__name__}[{error.__traceback__.tb_lineno}]: {error}"
            )
            raise GCPGetProjectError(file=__file__, original_exception=error)
        finally:
            return projects

    def update_projects_with_organizations(self):
        try:
            service = discovery.build(
                "cloudresourcemanager", "v1", credentials=self._session
            )
            # TODO: this call requires more permissions to get that data
            # resourcemanager.organizations.get --> add to the docs
            for project in self._projects.values():
                if project.organization:
                    request = service.organizations().get(
                        name=f"organizations/{project.organization.id}"
                    )

                    while request is not None:
                        response = request.execute()
                        project.organization.display_name = response.get("displayName")
                        request = service.projects().list_next(
                            previous_request=request, previous_response=response
                        )

        except HttpError as http_error:
            if http_error.status_code == 403 and "organizations" in http_error.uri:
                logger.error(
                    f"{http_error.__class__.__name__}[{http_error.__traceback__.tb_lineno}]: {http_error.error_details} to get Organizations display name."
                )
            else:
                logger.error(
                    f"{http_error.__class__.__name__}[{http_error.__traceback__.tb_lineno}]: {http_error}"
                )
        except Exception as error:
            logger.error(
                f"{error.__class__.__name__}[{error.__traceback__.tb_lineno}]: {error}"
            )

    def is_project_matching(self, input_project: str, project_to_match: str) -> bool:
        """
        Check if the input project matches the project to match
        Args:
            input_project: str
            project_to_match: str
        Returns:
            bool
        """
        return (
            "*" in input_project
            and re.search(
                "." + input_project if input_project.startswith("*") else input_project,
                project_to_match,
            )
        ) or input_project == project_to_match

    @staticmethod
    def validate_static_arguments(
        client_id: str = None, client_secret: str = None, refresh_token: str = None
    ) -> dict:
        """
        Validate the static arguments client_id, client_secret and refresh_token

        Args:
            client_id: str
            client_secret: str
            refresh_token: str

        Returns:
            dict

        Raises:
            GCPStaticCredentialsError if any of the static arguments is missing
        """

        if not client_id or not client_secret or not refresh_token:
            raise GCPStaticCredentialsError(
                file=__file__,
                message="client_id, client_secret and refresh_token are required.",
            )

        return {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "type": "authorized_user",
        }

    @staticmethod
    def validate_project_id(provider_id: str, credentials: str = None) -> None:
        """
        Validate the provider ID given the credentials, checking if the provider ID matches with the expected project_id using the method get_projects

        Args:
            provider_id: str
            credentials: str

        Returns:
            None

        Raises:
            GCPInvalidProviderIdError if the provider ID does not match with the expected project_id
        """

        available_projects = list(
            GcpProvider.get_projects(credentials=credentials).keys()
        )

        if len(available_projects) == 0:
            raise GCPNoAccesibleProjectsError(
                file=__file__,
                message="No Project IDs can be accessed via Google Credentials.",
            )
        elif provider_id not in available_projects:
            raise GCPInvalidProviderIdError(
                file=__file__,
                message="The provider ID does not match with the expected project_id.",
            )

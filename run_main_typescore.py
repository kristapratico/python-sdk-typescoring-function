# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

import os
import datetime
import logging
import requests
import json
from typing import Any
import azure.functions as func
from azure.data.tables import TableClient
from helpers import (
    get_packages_to_score,
    install,
    score_package,
    uninstall_deps,
    IGNORE_PACKAGES
)

IGNORE_PACKAGES.extend(["azure-ai-vision", "azure-ai-mlmonitoring"])  # not in our repo
logging.getLogger().setLevel(logging.INFO)

app = func.FunctionApp()

 # these packages have problems with dependency resolving when trying to 
 # install all the alpha packages in one venv. An second step is necessary
 # to install/score these packages separately
 # Format: "library_to_score": [dependencies_to_uninstall]
DEPENDENCY_ISSUE_LIBRARIES = {
    "azure-mixedreality-authentication": ["azure-mixedreality-remoterendering"],
    "azure-ai-ml": ["azure-storage-blob", "azure-storage-file-share", "azure-storage-file-datalake"],
    "azure-storage-blob-changefeed": ["azure-storage-blob"],
    "azure-storage-file-datalake": ["azure-storage-blob"],
    "azure-core": ["azure-core-experimental", "azure-core-tracing-opencensus", "azure-core-tracing-opentelemetry"]
}


def get_alpha_installs(packages_to_score: dict[str, Any]) -> tuple[list[str], list[str]]:
    response = requests.get("https://feeds.dev.azure.com/azure-sdk/public/_apis/packaging/feeds?api-version=7.0")

    feeds = json.loads(response.text)

    feed = [feed for feed in feeds["value"] if feed["name"] == "azure-sdk-for-python"][0]
    feed_resp = requests.get(f"https://feeds.dev.azure.com/azure-sdk/public/_apis/packaging/feeds/{feed['id']}/Packages?api-version=7.0")
    packages = json.loads(feed_resp.text)
    versions_to_install = {}

    for package in packages["value"]:
        if package["name"] in packages_to_score:
            url = f"{package['url']}/Versions"
            versions = requests.get(url)
            version_list = json.loads(versions.text)["value"]
            latest_publish_date = None
            for version in version_list:
                if "a" in version["version"]:
                    datetime_conv = datetime.datetime.strptime(version["publishDate"].split(".")[0], "%Y-%m-%dT%H:%M:%S")
                    if latest_publish_date is None:
                        latest_publish_date = datetime_conv
                        versions_to_install[package["name"]] = version["version"]
                    else:
                        if latest_publish_date < datetime_conv:
                            latest_publish_date = datetime_conv
                            versions_to_install[package["name"]] = version["version"]
    
    first_round = []
    second_round = {}
    for package_name, version in versions_to_install.items():
        packages_to_score[package_name].update({'LatestVersion': version})
        if package_name in DEPENDENCY_ISSUE_LIBRARIES:
            second_round[package_name] = [f"{package_name}=={version}", "--extra-index-url", "https://pkgs.dev.azure.com/azure-sdk/public/_packaging/azure-sdk-for-python/pypi/simple", "--pre"]
        else:
            first_round.extend([f"{package_name}=={version}", "--extra-index-url", "https://pkgs.dev.azure.com/azure-sdk/public/_packaging/azure-sdk-for-python/pypi/simple", "--pre"])
    return first_round, second_round


def main_typescore_function() -> None:
    client = TableClient.from_connection_string(os.getenv("CONNECTION_STRING_MAIN"), table_name="PythonSDKTypeScoreMain")

    packages_to_score = get_packages_to_score()

    first_round, second_round = get_alpha_installs(packages_to_score)

    install(first_round)

    entities = []
    for package, details in packages_to_score.items():
        if package in DEPENDENCY_ISSUE_LIBRARIES:
            continue
        score_package(package, packages_to_score, entities)

    second_round_entities = []
    for package, deps in DEPENDENCY_ISSUE_LIBRARIES.items():
        uninstall_deps(deps)
        install(second_round[package])
        score_package(package, packages_to_score, second_round_entities)

    entities.extend(second_round_entities)
    client.submit_transaction(entities)

main_typescore_function()

# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

import os
import datetime
import azure.functions as func
import logging
import json
from typing import Any
from azure.data.tables import TableClient
from azure.core.exceptions import HttpResponseError
from helpers import (
    get_packages_to_score,
    get_last_month,
    install,
    add_entity,
    score_package,
    uninstall_deps
)


logging.getLogger().setLevel(logging.INFO)

app = func.FunctionApp()


def get_released_installs(packages_to_score: dict[str, Any], client: TableClient) -> list[str]:
    install_packages = []
    today = datetime.datetime.today()

    for package_name, details in packages_to_score.items():
        try:
            # if the package didn't have a release, take the old score, don't bother running verifytypes
            entity = client.get_entity(partition_key=get_last_month(today), row_key=package_name)
            if entity["LatestVersion"] == packages_to_score[package_name]["LatestVersion"]:
                packages_to_score[package_name].update({"Score": entity["Score"]})
                packages_to_score[package_name].update({"PyTyped": entity["PyTyped"]})
                continue
        except HttpResponseError:
            pass

        install_packages.append(f"{package_name}=={packages_to_score[package_name]['LatestVersion']}")

    return install_packages


# @app.function_name(name="typescoretimer")
# @app.schedule(schedule="0 13 15 * *", arg_name="score", run_on_startup=True,
#               use_monitor=False)
# def released_typescore_function(score: func.TimerRequest) -> None:
def released_typescore_function() -> None:
    client = TableClient.from_connection_string(os.getenv("CONNECTION_STRING"), table_name="PythonSDKTypeScore")
    
    packages_to_score = get_packages_to_score()

    install_packages = get_released_installs(packages_to_score, client)

    install(install_packages)

    entities = []
    for package, details in packages_to_score.items():
        if package == "azure-core":
            continue
        if details.get("Score", None) is not None:
            add_entity(package, packages_to_score, entities)
            continue
        score_package(package, packages_to_score, entities)

    # special case for azure-core - score package w/o azure.core.experimental and azure.core.tracing
    uninstall_deps(["azure-core-experimental", "azure-core-tracing-opencensus", "azure-core-tracing-opentelemetry"])
    score_package("azure-core", packages_to_score, entities)
    client.submit_transaction(entities)

released_typescore_function()

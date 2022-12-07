# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

import os
import datetime
import azure.functions as func
import logging
import requests
import subprocess
import json
import sys
import pandas as pd
from typing import Any
from io import StringIO
from packaging.version import parse
from ci_tools.environment_exclusions import (
    PYRIGHT_OPT_OUT,
    MYPY_OPT_OUT,
    TYPE_CHECK_SAMPLES_OPT_OUT,
    VERIFYTYPES_OPT_OUT,
    IGNORE_PACKAGES,
    FILTER_EXCLUSIONS,
    IGNORE_FILTER,
)
from azure.data.tables import TableClient
from azure.core.exceptions import HttpResponseError

additional_ignores = [
    "adal",
    "msal",
    "pydocumentdb",
    "azure-devtools",
    "doc-warden",
    "azure-kusto-data",
    "msrest",
    "msrestazure",
    "tox-monorepo",
    "azure-functions",
    "iotedgedev",
    "iotedgehubdev",
    "iothub-client",
    "iothub-service-client",
    "text-analytics",
    "azure-storage-file",
    "azure-schemaregistry-avroserializer",
    "azure-iot-provisioning-device-client",
    "microsoft-opentelemetry-exporter-azuremonitor",
    "azure-search",
    "azureml-sdk",
    "azure-communication-administration",
    "azure-iot-device",
    "azure-purview-account",
    "azure-storage-common",
    "azure-iothub-provisioningserviceclient",
    "azure-iothub-service-client",
    "azure-cosmosdb-table",
    "azure-iothub-device-client",
    "azure-datalake-store",
    "azure-opentelemetry-exporter-azuremonitor",
    "azure-iot-hub",
    "uamqp",
]
IGNORE_PACKAGES.extend(additional_ignores)

logging.getLogger().setLevel(logging.INFO)

app = func.FunctionApp()


def is_ignored_package(package_name: str) -> bool:
    if package_name in IGNORE_PACKAGES:
        return True
    if package_name not in FILTER_EXCLUSIONS and any([identifier in package_name for identifier in IGNORE_FILTER]):
        return True
    return False


def get_last_month(today: datetime.datetime) -> str:
    month = today.month - 1
    year = today.year
    if month == 0:
        month = 12
        year = year-1
    last_month = datetime.date(year, month, today.day)
    return str(last_month)


def add_entity(package: str, packages_to_score: dict[str, Any], entities: tuple[str, dict[str, Any]]) -> None:
    d = packages_to_score[package]["Date"]
    entity = {
        "RowKey": package,
        "PartitionKey": str(datetime.date(d.year, d.month, d.day)),
        "Package": package,
        "Date": d,
        "LatestVersion": packages_to_score[package]["LatestVersion"],
        "Score": packages_to_score[package]["Score"],
        "PyTyped": packages_to_score[package]["PyTyped"],
        "Pyright": packages_to_score[package]["Pyright"],
        "Mypy": packages_to_score[package]["Mypy"],
        "Samples": packages_to_score[package]["Samples"],
        "Verifytypes": packages_to_score[package]["Verifytypes"],
    }
    entities.append(("create", entity))


def get_module(package: str) -> str:
    command = [sys.executable, "-m", "pip", "show", "-f", package]
    response = subprocess.run(
        command,
        check=True,
        capture_output=True,
    )
    resp = response.stdout.decode("utf-8")
    substring = resp[resp.find("Files:"):resp.find("__init__.py")]
    module = substring[substring.find("azure"):]
    module = module.replace("/", ".")
    module = module.replace("\\", ".")
    return module[:-1]


def install(packages: list[str]) -> None:
    if not packages:
        return
    # hacky, but we install pyright here
    # mismatch between python found by sys.executable
    # and python ran in the function
    packages.append("pyright==1.1.274")
    commands = [
        sys.executable,
        "-m",
        "pip",
        "install",
    ]

    commands.extend(packages)
    subprocess.check_call(commands)


@app.function_name(name="typescoretimer")
@app.schedule(schedule="0 13 15 * *", arg_name="score", run_on_startup=True,
              use_monitor=False)
def test_function(score: func.TimerRequest) -> None:
    logging.info('Python HTTP trigger function processed a request.')

    today = datetime.datetime.today()
    client = TableClient.from_connection_string(os.getenv("AzureWebJobsStorage"), table_name="PythonSDKTypeScore")
    response = requests.get("https://raw.githubusercontent.com/Azure/azure-sdk/main/_data/releases/latest/python-packages.csv")
    fields = ["Package", "VersionGA", "VersionPreview"]
    df = pd.read_csv(StringIO(response.text), sep=",", usecols=fields)
    df = df.reset_index()

    packages_to_score = {}
    install_packages = []
    for index, row in df.iterrows():
        package_name = row["Package"]
        # float represents NaN in csv
        if isinstance(package_name, float) or is_ignored_package(package_name) or package_name in packages_to_score:
            continue
        try:
            latest_version = str(max(parse(row["VersionGA"]), parse(row["VersionPreview"])))
        except TypeError:
            latest_version = row["VersionGA"] if not isinstance(row["VersionGA"], float) else row["VersionPreview"]

        packages_to_score[package_name] = {"LatestVersion": latest_version}
        packages_to_score[package_name].update({"Pyright": package_name not in PYRIGHT_OPT_OUT})
        packages_to_score[package_name].update({"Mypy": package_name not in MYPY_OPT_OUT})
        packages_to_score[package_name].update({"Samples": package_name not in TYPE_CHECK_SAMPLES_OPT_OUT})
        packages_to_score[package_name].update({"Verifytypes": package_name not in VERIFYTYPES_OPT_OUT})
        packages_to_score[package_name].update({"Date": today})
        try:
            # if the package didn't have a release, take the old score, don't bother running verifytypes
            entity = client.get_entity(partition_key=get_last_month(today), row_key=package_name)
            if entity["LatestVersion"] == packages_to_score[package_name]["LatestVersion"]:
                packages_to_score[package_name].update({"Score": entity["Score"]})
                packages_to_score[package_name].update({"PyTyped": entity["PyTyped"]})
                continue
        except HttpResponseError:
            pass

        install_packages.append(f"{package_name}=={latest_version}")

    install(install_packages)

    entities = []
    for package, details in packages_to_score.items():
        if details.get("Score", None) is not None:
            add_entity(package, packages_to_score, entities)
            continue

        module = get_module(package)
        try:
            logging.info(f"Running verifytypes on {package}")
            commands = [sys.executable, "-m", "pyright", "--verifytypes", module, "--ignoreexternal", "--outputjson"]
            response = subprocess.run(
                commands,
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            if e.returncode != 1:
                logging.info(
                    f"Running verifytypes for {package} failed: {e.stderr}"
                )
            else:
                report = json.loads(e.output)
        else:
            report = json.loads(response.stdout)  # package scores 100%
        pytyped_present = False if report["typeCompleteness"].get("pyTypedPath", None) is None else True
        packages_to_score[package].update({"PyTyped": pytyped_present})
        packages_to_score[package].update({"Score": round(report["typeCompleteness"]["completenessScore"] * 100, 1)})
        add_entity(package, packages_to_score, entities)

    client.submit_transaction(entities)

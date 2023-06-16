# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

import json
import datetime
import logging
import pandas as pd
import requests
import subprocess
import sys
from io import StringIO
from packaging.version import parse
from typing import Any
from ci_tools.environment_exclusions import (
    IGNORE_PACKAGES,
    FILTER_EXCLUSIONS,
    IGNORE_FILTER,
)


class TypingCheck:
    mypy: bool = True
    pyright: bool = True
    type_check_samples: bool = True
    verifytypes: bool = True


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
    "azureml-fsspec",
    "mltable",
    "apiview-stub-generator",
    "azure-pylint-guidelines-checker",
]
IGNORE_PACKAGES.extend(additional_ignores)

logging.getLogger().setLevel(logging.INFO)


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
    lines = resp.splitlines()
    for line in lines:
        if line.find("__init__.py") != -1:
            substring = line[line.find("azure"):line.find("__init__.py")]
            break
    module = substring.replace("/", ".")
    module = module.replace("\\", ".")
    return module[:-1]


def install(packages: list[str]) -> None:
    if not packages:
        return
    # hacky, but we install pyright here
    # mismatch between python found by sys.executable
    # and python ran in the function
    packages.append("pyright==1.1.287")
    commands = [
        sys.executable,
        "-m",
        "pip",
        "install",
    ]

    commands.extend(packages)
    subprocess.check_call(commands)


def uninstall_deps(deps: list[str]) -> None:
    commands = [
        sys.executable,
        "-m",
        "pip",
        "uninstall",
        "-y"
    ]

    commands.extend(deps)
    subprocess.check_call(commands)


def is_check_enabled(package_path: str) -> TypingCheck:
    toml_path = f"https://raw.githubusercontent.com/Azure/azure-sdk-for-python/main/sdk/{package_path}/pyproject.toml"
    response = requests.get(toml_path)
    if response.status_code == 404:
        # no pyproject.toml file -- library runs all checks
        return TypingCheck()

    is_enabled = TypingCheck()
    toml = response.text.split("\n")
    for line in toml:
        line = line.lower()
        if line.find("mypy") != -1 and line.find("false") != -1:
            is_enabled.mypy = False
        if line.find("pyright") != -1 and line.find("false") != -1:
            is_enabled.pyright = False
        if line.find("type_check_samples") != -1 and line.find("false") != -1:
            is_enabled.type_check_samples = False
        if line.find("verifytypes") != -1 and line.find("false") != -1:
            is_enabled.verifytypes = False
    return is_enabled


def score_package(package: str, packages_to_score: dict[str, Any], entities: tuple[str, dict[str, Any]]) -> None:
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


def get_packages_to_score() -> dict[str, Any]:
    today = datetime.datetime.today()
    # today = datetime.datetime(2023, 6, 15, 8, 18, 32, 486215)
    response = requests.get("https://raw.githubusercontent.com/Azure/azure-sdk/main/_data/releases/latest/python-packages.csv")
    fields = ["Package", "VersionGA", "VersionPreview", "RepoPath"]
    df = pd.read_csv(StringIO(response.text), sep=",", usecols=fields)
    df = df.reset_index()

    packages_to_score = {}
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
        package_path = f"{row['RepoPath']}/{row['Package']}"
        is_enabled = is_check_enabled(package_path)
        packages_to_score[package_name].update({"Pyright": is_enabled.pyright})
        packages_to_score[package_name].update({"Mypy": is_enabled.mypy})
        packages_to_score[package_name].update({"Samples": is_enabled.type_check_samples})
        packages_to_score[package_name].update({"Verifytypes": is_enabled.verifytypes})
        packages_to_score[package_name].update({"Date": today})
    return packages_to_score

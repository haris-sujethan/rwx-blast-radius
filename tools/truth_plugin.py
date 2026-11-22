"""pytest plugin: append the node id of every failed report (incl. subtests, errors) to $TRUTH_OUT."""

import os


def _write(nodeid):
    with open(os.environ["TRUTH_OUT"], "a") as f:
        f.write(nodeid + "\n")


def pytest_runtest_logreport(report):
    if report.failed:
        _write(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        _write(report.nodeid)

# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2.

from edenscm import extensions
from edenscm.ext.github import submit
from edenscm.ext.github.mock_utils import mock_run_git_command, MockGitHubServer
from ghstack import github_gh_cli

# An extension to mock network requests by replacing the `github_gh_cli.make_request`
# and `submit.run_git_command` with the corresponding wrapper functions. Check `uisetup`
# function for how wrapper functions are registered.


def setup_mock_github_server() -> MockGitHubServer:
    """Setup mock GitHub Server for testing happy case of `sl pr submit` command."""
    github_server = MockGitHubServer()

    github_server.expect_get_repository_request().and_respond()

    pr_number = 1
    github_server.expect_create_pr_placeholder_request().and_respond(
        start_number=pr_number, num_times=1
    )

    body = "addfile\n"
    github_server.expect_create_pr_request(body=body, issue=pr_number).and_respond()

    pr_id = f"PR_id_{pr_number}"
    github_server.expect_get_pr_details_request(pr_number).and_respond(pr_id)

    return github_server


def uisetup(ui):
    mock_github_server = setup_mock_github_server()
    extensions.wrapfunction(
        github_gh_cli, "make_request", mock_github_server.make_request
    )
    extensions.wrapfunction(submit, "run_git_command", mock_run_git_command)
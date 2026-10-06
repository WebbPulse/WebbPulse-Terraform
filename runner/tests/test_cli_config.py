"""The CLI config that answers `app.terraform.io` module lookups from this plane."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from app import cli_config
from app.models import RegistryCredentials

MODULES_URL = "https://api.staging.terraform.example.test/v1/modules/"


def test_render_points_modules_v1_at_the_plane() -> None:
    """One host block forces the host's `modules.v1` to the given URL."""
    assert cli_config.render({"app.terraform.io": MODULES_URL}) == (
        f'host "app.terraform.io" {{\n  services = {{\n    "modules.v1" = "{MODULES_URL}"\n  }}\n}}\n'
    )


def test_render_orders_hosts_and_holds_no_secret() -> None:
    """Hosts render sorted, and the file carries no credential block."""
    rendered = cli_config.render({"z.example.test": MODULES_URL, "app.terraform.io": MODULES_URL})

    assert rendered.index('host "app.terraform.io"') < rendered.index('host "z.example.test"')
    assert "credentials" not in rendered
    assert "token" not in rendered


@pytest.mark.parametrize(
    "host",
    ["", "App.Terraform.io", "app.terraform.io:443", 'app"terraform.io', "localhost", "app.terraform.io/x"],
)
def test_render_refuses_a_host_that_is_not_a_plain_hostname(host: str) -> None:
    """A host that could break out of its quoted label is refused."""
    with pytest.raises(cli_config.CliConfigError):
        cli_config.render({host: MODULES_URL})


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.test/v1/modules/",
        "/v1/modules/",
        "https:///v1/modules/",
        "https://api.example.test/${path}/",
        "https://api.example.test/%{x}/",
    ],
)
def test_render_refuses_a_url_that_is_not_plain_https(url: str) -> None:
    """Only an https URL with no HCL template sequence is written."""
    with pytest.raises(cli_config.CliConfigError):
        cli_config.render({"app.terraform.io": url})


def test_write_names_the_file_for_the_engine(tmp_path: Path) -> None:
    """The file lands under the given directory, owner only, and the variable names it."""
    environment = cli_config.write(tmp_path / "cli", {"app.terraform.io": MODULES_URL})

    path = Path(environment[cli_config.CONFIG_VARIABLE])
    assert path.parent == tmp_path / "cli"
    assert 'host "app.terraform.io"' in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_with_no_hosts_writes_nothing(tmp_path: Path) -> None:
    """No mapped host leaves the engine's own discovery alone."""
    assert cli_config.write(tmp_path / "cli", {}) == {}
    assert not (tmp_path / "cli").exists()


def test_the_credential_covers_every_mapped_host() -> None:
    """The run's registry token is set for the SPA host and for each mapped host."""
    credentials = RegistryCredentials(
        hosts=["terraform.example.test"],
        token="wpk_example-token",
        module_hosts={"app.terraform.io": MODULES_URL},
    )

    assert credentials.environment() == {
        "TF_TOKEN_terraform_example_test": "wpk_example-token",
        "TF_TOKEN_app_terraform_io": "wpk_example-token",
    }


def test_an_older_bundle_maps_no_host() -> None:
    """A credential without `module_hosts` sets the SPA host alone."""
    credentials = RegistryCredentials.model_validate({"hosts": ["terraform.example.test"], "token": "wpk_x"})

    assert credentials.module_hosts == {}
    assert list(credentials.environment()) == ["TF_TOKEN_terraform_example_test"]

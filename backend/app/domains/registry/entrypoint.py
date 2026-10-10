"""The registry domain's entrypoint, which is what this domain's Lambda runs.

`webbpulse.composition.domain_entrypoint` configures logging, then tracing, runs
the startup secrets check and serves the application this domain builds.
"""

from webbpulse.composition import domain_entrypoint

from app.common.composition.settings import get_settings
from app.common.composition.wiring import DOMAINS, build_domain_app

DOMAIN = DOMAINS["registry"]

build_app, main = domain_entrypoint(DOMAIN, build=build_domain_app, settings=get_settings)

if __name__ == "__main__":
    main()

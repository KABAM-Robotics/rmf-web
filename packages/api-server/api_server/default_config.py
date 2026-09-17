import os
from os.path import dirname


api_server_port = os.getenv("VITE_API_SERVER_PORT", "8000")
host_ip = os.getenv("VITE_HOST_IP", "localhost")
public_url = f"http://{host_ip}:{api_server_port}"
here = dirname(__file__)
run_dir = f"{here}/../run"


# pylint: disable=line-too-long
config = {
    # ip or hostname to bind the socket to, this only applies when running the server in
    # standalone mode.
    "host": "0.0.0.0",
    # port to bind to, this only applies when running the server in standalone mode.
    "port": api_server_port,
    # "port": "8000",
    "db_url": f"sqlite://{run_dir}/db.sqlite3",
    # url that rmf-server is being served on.
    # When being a proxy, this must be the url that rmf-server is mounted on.
    # E.g. https://example.com/rmf/api/v1
    "public_url": public_url,
    # "public_url": f"http://localhost:{api_server_port}",
    # "public_url": "http://localhost:8000",
    "cache_directory": f"{run_dir}/cache",  # The directory where cached files should be stored.
    "log_level": "INFO",  # https://docs.python.org/3.8/library/logging.html#levels
    # a user that is automatically given admin privileges, note that this does not guarantee that the user exists in the identity provider.
    "builtin_admin": "admin",
    # path to a PEM encoded RSA public key which is used to verify JWT tokens, if the path is relative, it is based on the working dir.
    "jwt_public_key": f"{here}/../../rmf-dashboard-framework/keycloak-example.pub",
    # jwt secret, this is mutually exclusive with `jwt_public_key`.
    "jwt_secret": None,
    # url to the oidc endpoint, used to authenticate rest requests, it should point to the well known endpoint, e.g.
    # http://localhost:8080/auth/realms/rmf-web/.well-known/openid-configuration.
    # NOTE: This is ONLY used for documentation purposes, the "jwt_public_key" will be the
    # only key used to verify a token.
    "oidc_url": None,
    # Audience the access token is meant for. Can also be an array.
    # Used to verify the "aud" claim.
    "aud": "rmf_api_server",
    # url or string that identifies the entity that issued the jwt token
    # Used to verify the "iss" claim
    "iss": f"https://{host_ip}:8443/realms/rmf-web",
    # Optional namespace prefix used as a fallback when looking up the
    # `preferred_username` claim. Some identity providers (verified for
    # Auth0) silently filter non-namespaced custom claims on standard OIDC
    # names from access tokens issued via the OAuth 2.0 `client_credentials`
    # (M2M) flow, in line with the collision-resistant-name guidance in
    # RFC 9068 §2.2.2 and RFC 7519 §4.2. When set, the authenticator will
    # look up `f"{preferred_username_claim_namespace}preferred_username"`
    # if the bare `preferred_username` claim is absent. The trailing slash
    # (or `/` separator) should be included in the namespace value if
    # desired, e.g. `"https://example.com/"` so the resolved claim becomes
    # `"https://example.com/preferred_username"`.
    "preferred_username_claim_namespace": None,
    # list of arguments passed to the ros node, "--ros-args" is automatically prepended to the list.
    # e.g.
    #   Run with sim time: ["-p", "use_sim_time:=true"]
    "ros_args": [],
    # Timezone at which the scheduler will operate in. This must be the same
    # as the system timezone, as well as the client UI timezone. Cross-timezone
    # scheduling is currently not supported.
    "timezone": "Asia/Singapore",
}

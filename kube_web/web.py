import aiohttp_jinja2
import jinja2
import csv
import json
import base64
import os
import pykube
import logging
from yarl import URL
import yaml

import pykube
from pykube import ObjectDoesNotExist
from pykube.objects import APIObject, NamespacedAPIObject, Namespace, Event
from aiohttp_session import SimpleCookieStorage, get_session, setup as session_setup
from aiohttp_session.cookie_storage import EncryptedCookieStorage
from aiohttp_remotes import XForwardedRelaxed
from aioauth_client import OAuth2Client
from cryptography.fernet import Fernet

from .cluster_manager import ClusterNotFound

from pathlib import Path

from aiohttp import web

from kube_web import __version__
from kube_web import kubernetes
from .resource_registry import ResourceRegistry

logging.basicConfig(level=logging.DEBUG)

logger = logging.getLogger(__name__)

HEALTH_PATH = "/health"
OAUTH2_CALLBACK_PATH = "/oauth2/callback"

CLUSTER_MANAGER = "cluster_manager"


routes = web.RouteTableDef()


def context():
    def decorator(func):
        async def func_wrapper(request):
            ctx = await func(request)
            if isinstance(ctx, dict) and "cluster" in ctx:
                cluster = request.app[CLUSTER_MANAGER].get(ctx["cluster"])
                namespaces = await kubernetes.get_list(Namespace.objects(cluster.api))
                ctx["namespaces"] = namespaces
                ctx["rel_url"] = request.rel_url
            return ctx

        return func_wrapper

    return decorator


@routes.get("/")
async def get_index(request):
    # we don't have anything to present on the homepage, so let's redirect to the cluster list
    raise web.HTTPFound(location="/clusters")


@routes.get("/clusters")
@aiohttp_jinja2.template("clusters.html")
async def get_clusters(request):
    return {"clusters": request.app[CLUSTER_MANAGER].clusters}


@routes.get("/clusters/{cluster}")
@aiohttp_jinja2.template("cluster.html")
@context()
async def get_cluster(request):
    cluster = request.app[CLUSTER_MANAGER].get(request.match_info["cluster"])
    namespaces = await kubernetes.get_list(Namespace.objects(cluster.api))
    return {
        "cluster": cluster.name,
        "namespace": None,
        "namespaces": namespaces,
        "resource_types": cluster.resource_registry.cluster_resource_types,
    }


@routes.get("/clusters/{cluster}/{plural}")
@aiohttp_jinja2.template("resource-list.html")
@context()
async def get_cluster_resource_list(request):
    cluster = request.app[CLUSTER_MANAGER].get(request.match_info["cluster"])
    plural = request.match_info["plural"]
    params = request.rel_url.query
    clazz = cluster.resource_registry.get_class_by_plural_name(plural, namespaced=False)
    if not clazz:
        raise web.HTTPNotFound(text="Resource type not found")
    table = await kubernetes.get_table(clazz.objects(cluster.api))
    if params.get("download") == "tsv":
        return await download_tsv(request, table)
    return {
        "cluster": cluster.name,
        "namespace": None,
        "plural": plural,
        "tables": [table],
    }


class ResponseWriter:
    def __init__(self, response):
        self.response = response
        self.data = ""

    def write(self, data):
        self.data += data

    async def flush(self):
        await self.response.write(self.data.encode("utf-8"))
        self.data = ""


async def as_tsv(table, fd) -> str:
    writer = csv.writer(fd, delimiter="\t", lineterminator="\n")
    writer.writerow(["Namespace"] + [col["name"] for col in table.columns])
    for row in table.rows:
        writer.writerow([row["object"]["metadata"]["namespace"]] + row["cells"])
        await fd.flush()


async def download_tsv(request, table):
    response = web.StreamResponse()
    response.content_type = "text/tab-separated-values"
    path = request.rel_url.path
    filename = path.strip("/").replace("/", "_")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}.tsv"'
    await response.prepare(request)
    await as_tsv(table, ResponseWriter(response))
    return response


@routes.get("/clusters/{cluster}/namespaces/{namespace}/{plural}")
@aiohttp_jinja2.template("resource-list.html")
@context()
async def get_namespaced_resource_list(request):
    cluster = request.app[CLUSTER_MANAGER].get(request.match_info["cluster"])
    namespace = request.match_info["namespace"]
    plural = request.match_info["plural"]

    is_all_namespaces = namespace == "_all"

    if plural == "all":
        # this list was extracted from kubectl get all --v=9
        resource_types = [
            "pods",
            "services",
            "daemonsets",
            "deployments",
            "replicasets",
            "statefulsets",
            "horizontalpodautoscalers",
            "jobs",
            "cronjobs",
        ]
    else:
        resource_types = plural.split(",")

    params = request.rel_url.query
    tables = []
    for _type in resource_types:
        clazz = cluster.resource_registry.get_class_by_plural_name(
            _type, namespaced=True
        )
        if not clazz:
            raise web.HTTPNotFound(text="Resource type not found")
        query = clazz.objects(cluster.api).filter(
            namespace=pykube.all if is_all_namespaces else namespace
        )
        if "selector" in params:
            query = query.filter(selector=params["selector"])

        table = await kubernetes.get_table(query)
        tables.append(table)
    if params.get("download") == "tsv":
        return await download_tsv(request, tables[0])
    return {
        "cluster": cluster.name,
        "namespace": namespace,
        "is_all_namespaces": is_all_namespaces,
        "plural": plural,
        "tables": tables,
    }


@routes.get("/clusters/{cluster}/{plural}/{name}")
@routes.get("/clusters/{cluster}/namespaces/{namespace}/{plural}/{name}")
@aiohttp_jinja2.template("resource-view.html")
@context()
async def get_resource_view(request):
    cluster = request.app[CLUSTER_MANAGER].get(request.match_info["cluster"])
    namespace = request.match_info.get("namespace")
    plural = request.match_info["plural"]
    name = request.match_info["name"]
    view = request.rel_url.query.get("view")
    clazz = cluster.resource_registry.get_class_by_plural_name(
        plural, namespaced=bool(namespace)
    )
    if not clazz:
        raise web.HTTPNotFound(text="Resource type not found")
    query = clazz.objects(cluster.api)
    if namespace:
        query = query.filter(namespace=namespace)
    resource = await kubernetes.get_by_name(query, name)

    field_selector = {
        "involvedObject.name": resource.name,
        "involvedObject.namespace": namespace or "",
        "involvedObject.kind": resource.kind,
        "involvedObject.uid": resource.metadata["uid"],
    }
    events = await kubernetes.get_list(
        Event.objects(cluster.api).filter(
            namespace=namespace or pykube.all, field_selector=field_selector
        )
    )

    if resource.kind == "Namespace":
        namespace = resource.name

    return {
        "cluster": cluster.name,
        "namespace": namespace,
        "plural": plural,
        "resource": resource,
        "view": view,
        "events": events,
    }


@routes.get(HEALTH_PATH)
async def get_health(request):
    return web.Response(text="OK")


def filter_yaml(value):
    return yaml.dump(value, default_flow_style=False)


def filter_highlight(value):
    from pygments import highlight
    from pygments.lexers import get_lexer_by_name
    from pygments.formatters import HtmlFormatter

    return highlight(value, get_lexer_by_name("yaml"), HtmlFormatter())


async def get_oauth2_client():
    authorize_url = URL(os.getenv("OAUTH2_AUTHORIZE_URL"))
    access_token_url = URL(os.getenv("OAUTH2_ACCESS_TOKEN_URL"))

    client_id = os.getenv("OAUTH2_CLIENT_ID")
    client_secret = os.getenv("OAUTH2_CLIENT_SECRET")

    client_id_file = os.getenv("OAUTH2_CLIENT_ID_FILE")
    if client_id_file:
        client_id = open(client_id_file).read().strip()
    client_secret_file = os.getenv("OAUTH2_CLIENT_SECRET_FILE")
    if client_secret_file:
        client_secret = open(client_secret_file).read().strip()

    # workaround for a bug in OAuth2Client where the authorize URL won't work with params ("?..")
    authorize_url_without_query = str(authorize_url.with_query(None))
    client = OAuth2Client(
        client_id=client_id,
        client_secret=client_secret,
        authorize_url=authorize_url_without_query,
        access_token_url=access_token_url,
    )
    return client, dict(authorize_url.query)


@web.middleware
async def auth(request, handler):
    path = request.rel_url.path
    if path == OAUTH2_CALLBACK_PATH:
        client, _ = await get_oauth2_client()
        # Get access token
        code = request.query["code"]
        try:
            state = json.loads(request.query["state"])
            original_url = state["url"]
        except:
            original_url = "/"
        redirect_uri = str(request.url.with_path(OAUTH2_CALLBACK_PATH))
        access_token, data = await client.get_access_token(
            code, redirect_uri=redirect_uri
        )
        session = await get_session(request)
        session["access_token"] = access_token
        raise web.HTTPFound(location=original_url)
    elif path != HEALTH_PATH:
        session = await get_session(request)
        if not session.get("access_token"):
            client, params = await get_oauth2_client()
            params["state"] = json.dumps({"url": str(request.rel_url)})
            raise web.HTTPFound(location=client.get_authorize_url(**params))
    response = await handler(request)
    return response


@web.middleware
async def error_handler(request, handler):
    try:
        response = await handler(request)
        return response
    except web.HTTPRedirection:
        raise
    except Exception as e:
        status = 500
        error_title = "Error"
        error_text = str(e)
        if isinstance(e, web.HTTPError):
            status = e.status
            error_text = e.text
        elif isinstance(e, ClusterNotFound):
            status = 404
            error_title = "Error: cluster not found"
            error_text = f'Cluster "{e.cluster}" not found'
        elif isinstance(e, ObjectDoesNotExist):
            status = 404
            error_title = "Error: object does not exist"
        context = {
            "error_title": error_title,
            "error_text": error_text,
            "status": status,
        }
        response = aiohttp_jinja2.render_template(
            "error.html", request, context, status=status
        )
        return response


def get_app(cluster_manager):
    app = web.Application()
    aiohttp_jinja2.setup(
        app, loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates"))
    )
    env = aiohttp_jinja2.get_env(app)
    env.filters.update(yaml=filter_yaml, highlight=filter_highlight)
    env.globals["version"] = __version__

    app.add_routes(routes)
    app.router.add_static("/assets", Path(__file__).parent / "templates" / "assets")

    # behind proxy
    app.middlewares.append(XForwardedRelaxed().middleware)

    secret_key = os.getenv("SESSION_SECRET_KEY") or Fernet.generate_key()
    secret_key = base64.urlsafe_b64decode(secret_key)
    session_setup(app, EncryptedCookieStorage(secret_key, cookie_name="KUBE_WEB_VIEW"))

    authorize_url = os.getenv("OAUTH2_AUTHORIZE_URL")
    access_token_url = os.getenv("OAUTH2_ACCESS_TOKEN_URL")

    if authorize_url and access_token_url:
        app.middlewares.append(auth)

    app.middlewares.append(error_handler)

    app[CLUSTER_MANAGER] = cluster_manager

    return app
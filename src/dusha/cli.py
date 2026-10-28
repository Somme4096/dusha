from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from . import homes, legacy
from .client import catalog, presets, remote, settings
from .providers import PLACEHOLDER, http_url

STATIC = frozenset({"init", "serve", "proxy", "provider", "list", "plugins"})
_TYPES = {"str": click.STRING, "int": click.INT, "float": click.FLOAT}
_BUILT = "dusha.commands"
_PROXY_NAME = "dusha.proxy.name"


class ConfigError(click.ClickException):
    exit_code = 2


def _print(value: Any) -> None:
    click.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _config(resolve: Callable[[], Any]) -> Any:
    try:
        return resolve()
    except (FileNotFoundError, ValueError) as error:
        raise ConfigError(str(error)) from error


def _remote(call: Callable[..., Any], *args: Any) -> Any:
    try:
        return call(*args)
    except remote.RemoteError as error:
        raise click.ClickException(str(error)) from error


def _selection(ctx: click.Context) -> dict[str, Any]:
    return ctx.find_root().params


def _client(ctx: click.Context) -> tuple[settings.Connection, Path | None]:
    params = _selection(ctx)
    return _config(
        lambda: settings.connection(
            homes.locate(params.get("config"), home=params.get("home"), companion=params.get("companion"))
        )
    )


def _param(spec: dict[str, Any]) -> click.Parameter:
    name = spec["name"]
    flag = "--" + name.replace("_", "-")
    kind, default, text = spec.get("type", "str"), spec.get("default"), spec.get("help", "")
    if kind == "bool":
        declaration = f"{flag}/--no-{flag[2:]}" if default else flag
        return click.Option([declaration, name], is_flag=True, default=bool(default), help=text)
    value = click.Choice(spec.get("choices") or []) if kind == "choice" else _TYPES.get(kind, click.STRING)
    repeated, required = bool(spec.get("repeated")), bool(spec.get("required"))
    if spec.get("option"):
        shown = default not in (None, "", [])
        return click.Option(
            [flag, name], type=value, default=default, required=required, multiple=repeated,
            show_default=shown, help=text,
        )
    return click.Argument([name], type=value, required=required, nargs=-1 if repeated else 1)


def _command(spec: dict[str, Any], connection: settings.Connection) -> click.Command:
    path = [part for part in (spec.get("group"), spec["name"]) if part]

    def callback(**values: Any) -> None:
        values = {key: list(value) if isinstance(value, tuple) else value for key, value in values.items()}
        _print(_remote(remote.run, connection, path, values))

    params = [_param(item) for item in spec.get("args", [])]
    return click.Command(spec["name"], callback=callback, params=params, help=spec.get("help", ""))


def _build(manifest: dict[str, Any], connection: settings.Connection) -> dict[str, click.Command]:
    built: dict[str, click.Command] = {}
    helps = manifest.get("groups", {})
    for spec in manifest.get("commands", []):
        group = spec.get("group")
        if (group or spec["name"]) in STATIC:
            continue
        if not group:
            built.setdefault(spec["name"], _command(spec, connection))
            continue
        target = built.setdefault(group, click.Group(group, help=helps.get(group, "")))
        if isinstance(target, click.Group):
            target.add_command(_command(spec, connection))
    return built


class Root(click.Group):
    def _dynamic(self, ctx: click.Context) -> dict[str, click.Command]:
        root = ctx.find_root()
        if _BUILT not in root.meta:
            try:
                connection, cache = _client(root)
                manifest, warning = _remote(remote.manifest, connection, cache)
                if warning:
                    click.echo(warning, err=True)
                root.meta[_BUILT] = _build(manifest, connection)
            except click.ClickException as error:
                root.meta[_BUILT] = error
        built = root.meta[_BUILT]
        if isinstance(built, click.ClickException):
            raise built
        return built

    def list_commands(self, ctx: click.Context) -> list[str]:
        try:
            dynamic = list(self._dynamic(ctx))
        except click.ClickException:
            dynamic = []
        return sorted({*super().list_commands(ctx), *dynamic})

    def get_command(self, ctx: click.Context, name: str) -> click.Command | None:
        return super().get_command(ctx, name) or self._dynamic(ctx).get(name)


@click.group(cls=Root, help=homes.DOCS, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("-c", "--companion", is_eager=True, help="Companion name under the dusha config root.")
@click.option("--home", is_eager=True, help="Companion home directory.")
@click.option("--config", is_eager=True, help="Configuration file path.")
def main(companion: str | None, home: str | None, config: str | None) -> None:
    pass


@main.command("serve", help="Run the server for one companion")
@click.argument("name", required=False)
@click.pass_context
def serve(ctx: click.Context, name: str | None) -> None:
    from .config import load_config

    params = _selection(ctx)
    companion = params.get("companion") or name

    def resolve() -> Any:
        if params.get("config") is None and (home := homes.named_home(params.get("home"), companion)):
            _init_serve(home)
        return load_config(params.get("config"), home=params.get("home"), companion=companion)

    cfg = _config(resolve)
    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info")


def _init_serve(home: Path) -> bool:
    from .config import ensure_server_config

    if done := ensure_server_config(home):
        click.echo(done, err=True)
    return bool(done)


def _init_proxy(path: Path, *, interactive: bool, url: str = "", token: str = "") -> bool:
    if url:
        click.echo(f"Saved {settings.save_server(path, url, token)}.", err=True)
    elif path.exists() and settings.server_saved(path):
        return False
    elif interactive:
        _connect(path)
    elif (local := settings.local_server(path)) is not None:
        click.echo(f"Saved {settings.save_server(path, local.url, '')}.", err=True)
    else:
        _setup(path)
    return True


def _upgrade(path: Path) -> bool:
    from .config import load_config

    changed = False
    if backup := legacy.upgrade_config_file(path):
        click.echo(f"moved proxy.upstreams to providers in {path}, the old file is {backup}", err=True)
        changed = True
    prompts = load_config(path).prompts.path if settings.local_server(path) is not None else ""
    if prompts and (backup := legacy.upgrade_prompts_file(Path(prompts))):
        click.echo(f"upgraded {prompts} to prompts schema 4, the old file is {backup}", err=True)
        changed = True
    return changed


@main.command("init", help="Create or update a companion home")
@click.argument("name", required=False)
@click.option("--serve", "serve_part", is_flag=True, help="Set up only the server keys.")
@click.option("--proxy", "proxy_part", is_flag=True, help="Set up only the proxy keys.")
@click.option("--server-url", default="", help="Server for the proxy, skips the prompt.")
@click.option("--token", default="", help="Token for --server-url.")
@click.pass_context
def init(
    ctx: click.Context, name: str | None, serve_part: bool, proxy_part: bool, server_url: str, token: str
) -> None:
    params = _selection(ctx)
    companion = params.get("companion") or name
    both = not serve_part and not proxy_part

    def resolve() -> bool:
        if params.get("config") is not None:
            raise ValueError("dusha init takes a companion name or --home, not --config")
        home = homes.named_home(params.get("home"), companion)
        if home is None:
            raise ValueError("name a companion, such as dusha init <name>")
        path = home / homes.CONFIG_FILE
        url = _url(server_url) if server_url else ""
        changed = _init_serve(home) if serve_part or both else False
        if proxy_part or both:
            changed = _init_proxy(path, interactive=sys.stdin.isatty(), url=url, token=token) or changed
        if path.exists():
            changed = _upgrade(path) or changed
        return changed

    if not _config(resolve):
        click.echo(f"{companion or params.get('home')} is up to date.", err=True)


def _url(value: str) -> str:
    try:
        return http_url(value, "the server URL")
    except ValueError as error:
        raise click.BadParameter(str(error)) from error


def _setup(path: Path) -> None:
    click.echo(f"No server saved for {path.parent.name} yet.", err=True)
    url = click.prompt("Server URL", default=settings.SERVER_URL, value_proc=_url, err=True)
    hidden = sys.stdin.isatty()
    token = click.prompt(
        "Server token, empty for none", default="", show_default=False, hide_input=hidden, err=True
    )
    click.echo(f"created {settings.create(path, url, token)}", err=True)


def _checked(value: str) -> bool | str:
    try:
        http_url(value.strip(), "the server URL")
    except ValueError as error:
        return str(error)
    return True


def _connect(path: Path) -> None:
    import questionary

    local = settings.local_server(path)
    url = settings.SERVER_URL
    click.echo(f"{path.parent.name} isn't connected to a server yet.", err=True)
    while True:
        here = local is not None and questionary.select("Connect to:", choices=[
            questionary.Choice(f"The server on this machine ({local.url})", value=True),
            questionary.Choice("Another server", value=False),
        ]).unsafe_ask()
        if here:
            url, token, probe = local.url, "", local
        else:
            typed = questionary.text("Server URL", default=url, validate=_checked).unsafe_ask()
            url = http_url(typed.strip(), "the server URL")
            token = questionary.password("Server token, empty for none").unsafe_ask()
            probe = settings.Connection(url, token)
        try:
            remote.plugins(probe)
        except remote.RemoteError as error:
            click.echo(str(error), err=True)
            choice = questionary.select("What now?", choices=["Try again", "Save anyway"]).unsafe_ask()
            if choice == "Try again":
                continue
        else:
            click.echo(f"Connected to {url}.", err=True)
        break
    click.echo(f"Saved {settings.save_server(path, url, token)}.", err=True)


class Proxy(click.Group):
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and not args[0].startswith("-") and args[0] not in self.commands:
            ctx.meta[_PROXY_NAME] = args.pop(0)
        return super().parse_args(ctx, args)


def _proxy_file(ctx: click.Context, *, setup: bool) -> Path:
    params = _selection(ctx)
    companion = params.get("companion") or ctx.meta.get(_PROXY_NAME)

    def resolve() -> Path:
        interactive = setup and sys.stdin.isatty()
        if params.get("config") is None and (home := homes.named_home(params.get("home"), companion)):
            if setup and not (home / homes.CONFIG_FILE).exists():
                _init_proxy(home / homes.CONFIG_FILE, interactive=interactive)
        path = homes.locate(params.get("config"), home=params.get("home"), companion=companion)
        if path is None:
            raise ValueError("name a companion, such as dusha proxy <name>")
        if interactive and not settings.server_saved(path):
            _connect(path)
        return path

    return _config(resolve)


@main.group(
    "proxy", cls=Proxy, invoke_without_command=True, subcommand_metavar="[NAME] [COMMAND] [ARGS]...",
    help="Run or configure the client proxy.",
)
@click.pass_context
def proxy(ctx: click.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    path = _proxy_file(ctx, setup=True)
    cfg = _config(lambda: settings.load(path))
    import uvicorn

    from .client.proxy import create_proxy_app

    uvicorn.run(create_proxy_app(cfg), host=cfg.host, port=cfg.port, log_level="info")


def _pick(found: dict[str, presets.Preset]) -> str:
    import questionary

    choices = [questionary.Choice(f"{item.name} ({item.id})", value=item.id) for item in found.values()]
    return questionary.select(
        "Provider", choices=choices, use_search_filter=True, use_jk_keys=False
    ).unsafe_ask()


def _provider_file(ctx: click.Context) -> Path:
    params = _selection(ctx)

    def resolve() -> Path:
        path = homes.locate(params.get("config"), home=params.get("home"), companion=params.get("companion"))
        if path is None:
            raise ValueError("name a companion, such as dusha -c <name> provider add")
        return path

    return _config(resolve)


def _add(path: Path, provider: str | None, profile: str | None, refresh: bool) -> None:
    try:
        data, warning = catalog.load(refresh=refresh)
    except catalog.CatalogError as error:
        raise click.ClickException(str(error)) from error
    if warning:
        click.echo(warning, err=True)
    found = presets.build(data)
    interactive = sys.stdin.isatty()
    if provider is None:
        if not interactive:
            raise ConfigError("Name a provider, such as dusha provider add deepseek")
        provider = _pick(found)
    if provider not in found:
        raise ConfigError(f"Unknown provider {provider!r}, see https://models.dev for the ids")
    preset, name = found[provider], profile or provider
    current = _config(lambda: settings.load(path, environ=False))
    if name in current.providers:
        click.confirm(f"Replace the provider {name}?", abort=True, err=True)
    values = {variable: click.prompt(variable, err=True) for variable in preset.variables}
    fallback = f"{{env:{preset.env}}}" if preset.env else ""
    key = click.prompt(
        f"API key for {preset.name}, Enter for {fallback or 'none'}", default="", show_default=False,
        hide_input=interactive, err=True,
    )
    raw = preset.profile(key or fallback, values)
    _config(lambda: settings.save_provider(path, name, raw))
    companion, host = path.parent.name, f"[{current.host}]" if ":" in current.host else current.host
    harness = f"http://{host}:{current.port}/{name}"
    click.echo(f"Success!\nNew provider {name} added to {companion} at {harness}.", err=True)
    click.echo(f"Restart dusha proxy {companion} and dusha serve {companion} to use it.", err=True)


def _remove(path: Path, profile: str) -> None:
    if profile not in _config(lambda: settings.load(path, environ=False)).providers:
        raise click.ClickException(f"No provider named {profile!r}")
    _config(lambda: settings.save_provider(path, profile, None))
    click.echo(f"Removed {profile} from {path.parent.name}", err=True)


def _masked(key: str) -> str:
    if not key or PLACEHOLDER.search(key):
        return key
    return f"...{key[-4:]}" if len(key) > 8 else "..."


def _list(path: Path) -> None:
    cfg = _config(lambda: settings.load(path, environ=False))
    _print({
        name: {
            "base_url": profile.base_url, "endpoints": profile.endpoints, "auth": profile.auth,
            "key": _masked(profile.key),
        }
        for name, profile in cfg.providers.items()
    })


@main.group("provider", help="Manage the model providers of a companion")
def provider_group() -> None:
    pass


@provider_group.command("add", help="Add a provider from the models.dev catalog")
@click.argument("provider", required=False)
@click.option("--profile", help="Profile name, the provider id by default.")
@click.option("--refresh", is_flag=True, help="Refetch the models.dev catalog.")
@click.pass_context
def provider_add(ctx: click.Context, provider: str | None, profile: str | None, refresh: bool) -> None:
    _add(_provider_file(ctx), provider, profile, refresh)


@provider_group.command("remove", help="Remove a provider")
@click.argument("profile")
@click.pass_context
def provider_remove(ctx: click.Context, profile: str) -> None:
    _remove(_provider_file(ctx), profile)


@provider_group.command("list", help="Show the providers, keys masked")
@click.pass_context
def provider_list(ctx: click.Context) -> None:
    _list(_provider_file(ctx))


# Deprecated dusha proxy <name> auth, removed in 2.0.21.
@proxy.group("auth", hidden=True)
def auth() -> None:
    click.echo("dusha proxy auth is deprecated and goes away in 2.0.21. Use dusha provider.", err=True)


@auth.command("login")
@click.argument("provider", required=False)
@click.option("--profile")
@click.option("--refresh", is_flag=True)
@click.pass_context
def login(ctx: click.Context, provider: str | None, profile: str | None, refresh: bool) -> None:
    _add(_proxy_file(ctx, setup=True), provider, profile, refresh)


@auth.command("logout")
@click.argument("profile")
@click.pass_context
def logout(ctx: click.Context, profile: str) -> None:
    _remove(_proxy_file(ctx, setup=False), profile)


@auth.command("status")
@click.pass_context
def status(ctx: click.Context) -> None:
    _list(_proxy_file(ctx, setup=False))


@main.command("list", help="List companions")
def list_companions() -> None:
    _print({"root": str(homes.config_root()), "companions": homes.list_companions()})


@main.command("plugins", help="Show plugin states")
@click.pass_context
def plugins(ctx: click.Context) -> None:
    connection, _ = _client(ctx)
    _print(_remote(remote.plugins, connection))


if __name__ == "__main__":
    main(prog_name="dusha")

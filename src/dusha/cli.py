from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from . import homes
from .client import remote, settings

STATIC = frozenset({"serve", "proxy", "list", "plugins"})
_TYPES = {"str": click.STRING, "int": click.INT, "float": click.FLOAT}
_BUILT = "dusha.commands"


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


@main.command("serve", help="Run the server for one companion, the state API plus /mcp")
@click.argument("name", required=False)
@click.pass_context
def serve(ctx: click.Context, name: str | None) -> None:
    from .config import create_default_config, load_config

    params = _selection(ctx)
    companion = params.get("companion") or name

    def resolve() -> Any:
        if params.get("config") is None and (home := homes.named_home(params.get("home"), companion)):
            if not (home / homes.CONFIG_FILE).exists():
                click.echo(f"created {create_default_config(home)}", err=True)
        return load_config(params.get("config"), home=params.get("home"), companion=companion)

    cfg = _config(resolve)
    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info")


def _url(value: str) -> str:
    try:
        return settings.http_url(value, "the server URL")
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


@main.command("proxy", help="Run the client proxy next to your harness")
@click.argument("name", required=False)
@click.pass_context
def proxy(ctx: click.Context, name: str | None) -> None:
    params = _selection(ctx)
    companion = params.get("companion") or name

    def resolve() -> settings.Settings:
        if params.get("config") is None and (home := homes.named_home(params.get("home"), companion)):
            if not (home / homes.CONFIG_FILE).exists():
                _setup(home / homes.CONFIG_FILE)
        path = homes.locate(params.get("config"), home=params.get("home"), companion=companion)
        if path is None:
            raise ValueError("name a companion, such as dusha proxy <name>")
        return settings.load(path)

    cfg = _config(resolve)
    import uvicorn

    from .client.proxy import create_proxy_app

    uvicorn.run(create_proxy_app(cfg), host=cfg.host, port=cfg.port, log_level="info")


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

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
from collections.abc import Callable
from gettext import gettext
from pathlib import Path
from typing import Any

import click

from . import __version__, homes, providers
from .client import catalog, presets, remote, settings
from .providers import PLACEHOLDER, http_url

STATIC = frozenset({"init", "serve", "proxy", "provider", "companion", "plugin"})
_ROOT_VALUE_OPTIONS = ("-c", "--companion", "--home", "--config")
_ROOT_ATTACHED_OPTIONS = ("--companion=", "--home=", "--config=")
_ROOT_FLAG_OPTIONS = ("-v", "--verbose", "-q", "--quiet")
_TYPES = {"str": click.STRING, "int": click.INT, "float": click.FLOAT}
_BUILT = "dusha.commands"
_CORE = "dusha.core"
_PROXY_NAME = "dusha.proxy.name"
_HEADING, _TERM, _DONE, _WARNING, _ERROR = "green", "cyan", "green", "yellow", "red"
_logger = logging.getLogger("dusha.cli")


def _paint(text: str, **style: Any) -> str:
    return text if os.environ.get("NO_COLOR") else click.style(text, **style)


def _sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return text
    first = text.split(None, 1)[0]
    if text[0].islower() and "_" not in first and not first.startswith("-"):
        text = text[0].upper() + text[1:]
    if text[-1] not in ".!?":
        text = f"{text}."
    return text


def _block(title: str, color: str, *lines: str) -> str:
    header = _paint(title, fg=color, bold=True)
    body = [_sentence(line) for line in lines if line.strip()]
    return "\n".join((header, *body)) if body else header


def _warn(*lines: str) -> str:
    return _block("Warning!", _WARNING, *lines)


def _success(*lines: str) -> str:
    return _block("Success!", _DONE, *lines)


def _is_quiet(ctx: click.Context | None = None) -> bool:
    current = ctx or click.get_current_context(silent=True)
    return bool(current and current.find_root().params.get("quiet"))


def _echo_warn(*lines: str, ctx: click.Context | None = None) -> None:
    if not _is_quiet(ctx):
        click.echo(_warn(*lines), err=True)


def _echo_success(*lines: str, ctx: click.Context | None = None) -> None:
    if not _is_quiet(ctx):
        click.echo(_success(*lines), err=True)


def _ask(text: str) -> str:
    return f"{_paint('?', fg=_WARNING, bold=True)} {_paint(text, bold=True)}"


def _prompt_style() -> Any:
    import questionary

    return questionary.Style([
        ("qmark", f"fg:ansi{_WARNING} bold"),
        ("answer", f"fg:ansi{_TERM}"),
        ("pointer", f"fg:ansi{_TERM} bold"),
        ("highlighted", f"fg:ansi{_TERM} bold"),
        ("search_success", f"noinherit fg:ansi{_DONE} bold"),
        ("search_none", f"noinherit fg:ansi{_ERROR} bold"),
    ])


def _term(term: str) -> str:
    def paint(piece: str) -> str:
        name = piece.startswith("-") or not term.startswith("-")
        return _paint(piece, fg=_TERM, bold=True if name else None)

    pieces = re.split(r"(, | / | )", term)
    return "".join(piece if index % 2 else paint(piece) for index, piece in enumerate(pieces))


class Styled(click.HelpFormatter):
    def write_usage(self, prog: str, args: str = "", prefix: str | None = None) -> None:
        label = _paint(prefix or "Usage: ", fg=_HEADING, bold=True)
        super().write_usage(_paint(prog, fg=_TERM, bold=True), args and _paint(args, fg=_TERM), label)

    def write_heading(self, heading: str) -> None:
        self.write(f"{'':>{self.current_indent}}{_paint(heading + ':', fg=_HEADING, bold=True)}\n")

    def write_dl(self, rows: Any, col_max: int = 30, col_spacing: int = 2) -> None:
        super().write_dl([(_term(term), text) for term, text in rows], col_max, col_spacing)


def _translate(text: str) -> str:
    return gettext(text).replace("Error: ", f"{_paint('Error!', fg=_ERROR, bold=True)}\n", 1)


click.Context.formatter_class = Styled
click.exceptions._ = _translate


class ConfigError(click.ClickException):
    exit_code = 2


def _print(value: Any) -> None:
    click.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _sentences(text: str) -> str:
    return "\n".join(_sentence(line) for line in text.splitlines() if line.strip())


def _config(resolve: Callable[[], Any], *, hint: str = "") -> Any:
    try:
        return resolve()
    except FileNotFoundError as error:
        _logger.debug("Configuration lookup failed", exc_info=error)
        message = _sentences(str(error))
        raise ConfigError(f"{message}\n{_sentence(hint)}" if hint else message) from error
    except ValueError as error:
        _logger.debug("Configuration error", exc_info=error)
        raise ConfigError(_sentences(str(error))) from error


def _add_hint(companion: str | None, home: str | None) -> str:
    if companion:
        return f"Run dusha companion add {companion} first."
    if home:
        return f"Run dusha companion add --home {home} first."
    return "Run dusha companion add <name> first."


def _remote(call: Callable[..., Any], *args: Any) -> Any:
    try:
        return call(*args)
    except remote.RemoteError as error:
        _logger.debug("Remote request failed", exc_info=error)
        raise click.ClickException(_sentence(str(error))) from error


def _selection(ctx: click.Context) -> dict[str, Any]:
    return ctx.find_root().params


def _companion_name(ctx: click.Context, name: str | None) -> str | None:
    chosen = _selection(ctx).get("companion")
    if chosen and name and chosen != name:
        raise click.UsageError(f"Specify one companion, got both {chosen!r} and {name!r}.", ctx=ctx)
    return chosen or name


def _client(ctx: click.Context, companion: str | None = None) -> tuple[settings.Connection, Path | None]:
    params = _selection(ctx)
    return _config(
        lambda: settings.connection(
            homes.locate(
                params.get("config"),
                home=params.get("home"),
                companion=_companion_name(ctx, companion),
            )
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


def _command(
    spec: dict[str, Any], connection: settings.Connection, offline: remote.Unreachable | None
) -> click.Command:
    path = [part for part in (spec.get("group"), spec["name"]) if part]

    def callback(**values: Any) -> None:
        if offline:
            _logger.debug("Server offline for command %s", " ".join(path), exc_info=offline)
            raise click.ClickException(_sentence(str(offline)))
        values = {key: list(value) if isinstance(value, tuple) else value for key, value in values.items()}
        _print(_remote(remote.run, connection, path, values))

    params = [_param(item) for item in spec.get("args", [])]
    return click.Command(spec["name"], callback=callback, params=params, help=spec.get("help", ""))


def _build(
    manifest: dict[str, Any], connection: settings.Connection, offline: remote.Unreachable | None
) -> dict[str, click.Command]:
    built: dict[str, click.Command] = {}
    helps = manifest.get("groups", {})
    for spec in manifest.get("commands", []):
        group = spec.get("group")
        if (group or spec["name"]) in STATIC:
            continue
        if not group:
            built.setdefault(spec["name"], _command(spec, connection, offline))
            continue
        target = built.setdefault(group, click.Group(group, help=helps.get(group, "")))
        if isinstance(target, click.Group):
            target.add_command(_command(spec, connection, offline))
    return built


def _configure_logging(verbose: bool, quiet: bool) -> None:
    logger = logging.getLogger("dusha")
    for handler in list(logger.handlers):
        if getattr(handler, "_dusha_cli", False):
            logger.removeHandler(handler)
    if verbose:
        logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler(sys.stderr)
        handler._dusha_cli = True  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
    elif quiet:
        logger.setLevel(logging.ERROR)
    else:
        logger.setLevel(logging.WARNING)


def _verbosity_callback(ctx: click.Context, param: click.Parameter, value: bool) -> bool:
    if not value:
        return value
    other = "quiet" if param.name == "verbose" else "verbose"
    if ctx.params.get(other):
        raise click.UsageError("Use only one of --verbose or --quiet.", ctx=ctx)
    _configure_logging(param.name == "verbose", param.name == "quiet")
    return value


def _uvicorn_level(ctx: click.Context) -> str:
    params = _selection(ctx)
    if params.get("verbose"):
        return "debug"
    if params.get("quiet"):
        return "warning"
    return "info"


def _hoist_root_options(ctx: click.Context, args: list[str]) -> list[str]:
    hoisted: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            rest.extend(args[index:])
            break
        if token in _ROOT_VALUE_OPTIONS:
            if index + 1 >= len(args):
                raise click.BadOptionUsage(token, f"Option '{token}' requires an argument.", ctx)
            hoisted.extend((token, args[index + 1]))
            index += 2
            continue
        if (
            token in _ROOT_FLAG_OPTIONS
            or token.startswith(_ROOT_ATTACHED_OPTIONS)
            or (token.startswith("-c") and len(token) > 2)
        ):
            hoisted.append(token)
            index += 1
            continue
        rest.append(token)
        index += 1
    return [*hoisted, *rest]


class Root(click.Group):
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        rest = super().parse_args(ctx, _hoist_root_options(ctx, args))
        verbose, quiet = bool(ctx.params.get("verbose")), bool(ctx.params.get("quiet"))
        if verbose and quiet:
            raise click.UsageError("Use only one of --verbose or --quiet.", ctx=ctx)
        _configure_logging(verbose, quiet)
        return rest

    def _dynamic(self, ctx: click.Context) -> dict[str, click.Command]:
        root = ctx.find_root()
        if _BUILT not in root.meta:
            try:
                connection, cache = _client(root)
                manifest, stale = _remote(remote.manifest, connection, cache)
                if stale:
                    _echo_warn("Using the cached command list.", str(stale), ctx=root)
                offline = stale if isinstance(stale, remote.Unreachable) else None
                root.meta[_BUILT] = _build(manifest, connection, offline)
                root.meta[_CORE] = frozenset(manifest.get("core", []))
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

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        sections: dict[str, list[tuple[str, str]]] = {"Core commands": [], "Plugin commands": []}
        names = self.list_commands(ctx)
        failed = ctx.find_root().meta.get(_BUILT)
        if isinstance(failed, click.ClickException) and isinstance(failed.__cause__, remote.Unreachable):
            _echo_warn(failed.format_message(), "Plugin commands are hidden.", ctx=ctx)
        core = {*self.commands, *ctx.find_root().meta.get(_CORE, ())}
        limit = formatter.width - 6 - max(map(len, names), default=0)
        for name in names:
            command = self.get_command(ctx, name)
            if command and not command.hidden:
                section = "Core commands" if name in core else "Plugin commands"
                sections[section].append((name, command.get_short_help_str(limit)))
        for title, rows in sections.items():
            if rows:
                with formatter.section(title):
                    formatter.write_dl(rows)


@click.group(cls=Root, help=homes.DOCS, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(
    __version__, "-V", "--version", prog_name="dusha", message="%(prog)s %(version)s",
    help="Show the version and exit.",
)
@click.option("-c", "--companion", is_eager=True, help="Companion name under the dusha config root.")
@click.option("--home", is_eager=True, help="Companion home directory.")
@click.option("--config", is_eager=True, help="Configuration file path.")
@click.option(
    "-v", "--verbose", is_flag=True, is_eager=True, callback=_verbosity_callback,
    help="Enable debug logging and detailed errors.",
)
@click.option(
    "-q", "--quiet", is_flag=True, is_eager=True, callback=_verbosity_callback,
    help="Suppress warnings and status messages.",
)
def main(
    companion: str | None, home: str | None, config: str | None, verbose: bool, quiet: bool
) -> None:
    pass


@main.command("serve", help="Run the server for a companion")
@click.argument("name", required=False)
@click.pass_context
def serve(ctx: click.Context, name: str | None) -> None:
    from .config import load_config

    params = _selection(ctx)
    companion = _companion_name(ctx, name)

    def resolve() -> Any:
        path = homes.locate(params.get("config"), home=params.get("home"), companion=companion)
        if path is None:
            raise ValueError("Specify a companion, such as dusha serve <name>.")
        return load_config(path)

    hint = "" if params.get("config") is not None else _add_hint(companion, params.get("home"))
    cfg = _config(resolve, hint=hint)
    import uvicorn

    from .api import create_app

    host = os.getenv("DUSHA_HOST") or cfg.host
    uvicorn.run(create_app(cfg), host=host, port=cfg.port, log_level=_uvicorn_level(ctx))


def _change_text(change: Any) -> str:
    from . import setup

    templates = {
        setup.CREATED: f"Created {change.path}.",
        setup.ADDED_SERVER: f"Added the server keys to {change.path}.",
        setup.ADDED_PROXY: f"Saved {change.path}.",
        setup.UPGRADED_CONFIG: (
            f"Moved proxy.upstreams to providers in {change.path}, the old file is {change.backup}."
        ),
        setup.UPGRADED_PROMPTS: (
            f"Upgraded {change.path} to prompts schema 4, the old file is {change.backup}."
        ),
    }
    return templates[change.kind]


def _changes(changes: list[Any], *, interactive: bool = False, upgrade: bool = False) -> bool:
    from . import setup

    notes: list[str] = []
    for change in changes:
        if change.kind == setup.NEEDS_PROXY:
            if notes:
                _echo_success(*notes)
                notes.clear()
            _prompt_proxy(change.path, interactive)
            if upgrade and change.path.exists():
                notes.extend(_change_text(item) for item in setup.upgrade_files(change.path))
        else:
            notes.append(_change_text(change))
    if notes:
        _echo_success(*notes)
    return bool(changes)


def _prompt_proxy(path: Path, interactive: bool) -> None:
    if interactive:
        _connect(path)
    else:
        _setup(path)


def _companion_home(
    ctx: click.Context, name: str | None, command: str
) -> tuple[Path, str | None, str | None]:
    params = _selection(ctx)
    companion = _companion_name(ctx, name)
    home_arg = params.get("home")
    if params.get("config") is not None:
        raise ConfigError(f"Pass a companion name or --home to dusha {command}, not --config.")
    home = _config(lambda: homes.named_home(home_arg, companion))
    if home is None:
        raise ConfigError(f"Specify a companion, such as dusha {command} <name>.")
    return home, companion, home_arg


def _configure_companion(
    ctx: click.Context,
    name: str | None,
    *,
    serve_part: bool,
    proxy_part: bool,
    server_url: str,
    token: str,
    command: str,
    mode: str,
) -> None:
    from .setup import ensure_config

    home, companion, home_arg = _companion_home(ctx, name, command)
    path = home / homes.CONFIG_FILE
    target = companion or f"--home {home_arg}"
    label = companion or str(home_arg)
    if mode == "add" and path.exists():
        raise ConfigError(
            f"Companion {label} already exists.\nUse dusha companion update {target} to update it."
        )
    if mode == "update" and not path.exists():
        raise ConfigError(
            f"Configuration file not found: {path}.\nRun dusha companion add {target} first."
        )
    both = not serve_part and not proxy_part

    def resolve() -> bool:
        url = _url(server_url) if server_url else ""
        interactive = sys.stdin.isatty()
        changes = ensure_config(
            home,
            path,
            server=serve_part or both,
            proxy=proxy_part or both,
            url=url,
            token=token,
            interactive=interactive,
        )
        return _changes(changes, interactive=interactive, upgrade=True)

    if not _config(resolve):
        note = f"Companion {companion} is up to date." if companion else f"Home {home_arg} is up to date."
        _echo_success(note)


# Deprecated dusha init, removed in 2.0.21.
@main.command("init", hidden=True)
@click.argument("name", required=False)
@click.option("--serve", "serve_part", is_flag=True)
@click.option("--proxy", "proxy_part", is_flag=True)
@click.option("--server-url", default="")
@click.option("--token", default="")
@click.pass_context
def init(
    ctx: click.Context, name: str | None, serve_part: bool, proxy_part: bool, server_url: str, token: str
) -> None:
    _echo_warn(
        "Command dusha init is deprecated and goes away in 2.0.21.",
        "Use dusha companion add or dusha companion update.",
    )
    _configure_companion(
        ctx,
        name,
        serve_part=serve_part,
        proxy_part=proxy_part,
        server_url=server_url,
        token=token,
        command="init",
        mode="either",
    )


def _url(value: str) -> str:
    try:
        return http_url(value, "the server URL")
    except ValueError as error:
        raise click.BadParameter(_sentence(str(error))) from error


def _setup(path: Path) -> None:
    click.echo(f"No server saved for {path.parent.name} yet.", err=True)
    url = click.prompt(_ask("Server URL"), default=settings.SERVER_URL, value_proc=_url, err=True)
    hidden = sys.stdin.isatty()
    token = click.prompt(
        _ask("Server token, empty for none"), default="", show_default=False, hide_input=hidden, err=True
    )
    _echo_success(f"Created {settings.create(path, url, token)}.")


def _checked(value: str) -> bool | str:
    try:
        http_url(value.strip(), "the server URL")
    except ValueError as error:
        return _sentence(str(error))
    return True


def _connect(path: Path) -> None:
    import questionary

    local = settings.local_server(path)
    url = settings.SERVER_URL
    style = _prompt_style()
    click.echo(f"Companion {path.parent.name} isn't connected to a server yet.", err=True)
    connected = False
    while True:
        here = local is not None and questionary.select("Connect to:", choices=[
            questionary.Choice(f"The server on this machine ({local.url})", value=True),
            questionary.Choice("Another server", value=False),
        ], style=style).unsafe_ask()
        if here:
            url, token, probe = local.url, "", local
        else:
            typed = questionary.text("Server URL", default=url, validate=_checked, style=style).unsafe_ask()
            url = http_url(typed.strip(), "the server URL")
            token = questionary.password("Server token, empty for none", style=style).unsafe_ask()
            probe = settings.Connection(url, token)
        try:
            remote.plugins(probe)
        except remote.RemoteError as error:
            _echo_warn(str(error))
            choice = questionary.select(
                "What now?", choices=["Try again", "Save anyway"], style=style
            ).unsafe_ask()
            if choice == "Try again":
                continue
        else:
            connected = True
        break
    lines = [f"Connected to {url}."] if connected else []
    lines.append(f"Saved {settings.save_server(path, url, token)}.")
    _echo_success(*lines)


class Proxy(click.Group):
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and not args[0].startswith("-") and args[0] not in self.commands:
            ctx.meta[_PROXY_NAME] = args.pop(0)
        return super().parse_args(ctx, args)


def _proxy_file(ctx: click.Context) -> Path:
    params = _selection(ctx)
    companion = _companion_name(ctx, ctx.meta.get(_PROXY_NAME))

    def resolve() -> Path:
        path = homes.locate(params.get("config"), home=params.get("home"), companion=companion)
        if path is None:
            raise ValueError("Specify a companion, such as dusha proxy <name>.")
        return path

    hint = "" if params.get("config") is not None else _add_hint(companion, params.get("home"))
    return _config(resolve, hint=hint)


@main.group(
    "proxy", cls=Proxy, invoke_without_command=True, subcommand_metavar="[NAME]",
    help="Run or configure the proxy",
)
@click.pass_context
def proxy(ctx: click.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    path = _proxy_file(ctx)
    cfg = _config(lambda: settings.load(path))
    import uvicorn

    from .client.proxy import create_proxy_app

    uvicorn.run(create_proxy_app(cfg), host=cfg.host, port=cfg.port, log_level=_uvicorn_level(ctx))


CUSTOM = "custom"


def _pick(found: dict[str, presets.Preset]) -> str:
    import questionary

    choices = [questionary.Choice(f"{item.name} ({item.id})", value=item.id) for item in found.values()]
    choices.append(questionary.Choice("Custom", value=CUSTOM))
    return questionary.select(
        "Provider", choices=choices, use_search_filter=True, use_jk_keys=False, style=_prompt_style()
    ).unsafe_ask()


def _provider_file(ctx: click.Context, name: str | None = None) -> Path:
    params = _selection(ctx)
    companion = _companion_name(ctx, name)

    def resolve() -> Path:
        path = homes.locate(params.get("config"), home=params.get("home"), companion=companion)
        if path is None:
            raise ValueError("Specify a companion, such as dusha provider <name>.")
        return path

    hint = "" if params.get("config") is not None else _add_hint(companion, params.get("home"))
    return _config(resolve, hint=hint)


def _add(path: Path, provider: str | None, profile: str | None, refresh: bool) -> None:
    try:
        data, warning = catalog.load(refresh=refresh)
    except catalog.CatalogError as error:
        raise click.ClickException(_sentence(str(error))) from error
    if warning:
        _echo_warn(warning)
    found = presets.build(data)
    interactive = sys.stdin.isatty()
    if provider is None:
        if not interactive:
            raise ConfigError("Add a provider, such as dusha provider --add deepseek.")
        provider = _pick(found)
    if provider == CUSTOM:
        _custom(path, profile)
        return
    if provider not in found:
        raise ConfigError(f"Unknown provider {provider!r}, see https://models.dev for the ids.")
    preset, name = found[provider], profile or provider
    _replaceable(path, name)
    values = {variable: click.prompt(_ask(variable), err=True) for variable in preset.variables}
    key = click.prompt(
        _ask(f"API key for {preset.name}, Enter for none"), default="", show_default=False,
        hide_input=interactive, err=True,
    )
    _save(path, name, preset.profile(key, values))


def _replaceable(path: Path, name: str) -> None:
    if name in _config(lambda: settings.load(path, environ=False)).providers:
        click.confirm(_ask(f"Replace the provider {name}?"), abort=True, err=True)


def _save(path: Path, name: str, raw: dict[str, Any]) -> None:
    current = _config(lambda: settings.load(path, environ=False))
    _config(lambda: settings.save_provider(path, name, raw))
    companion, host = path.parent.name, f"[{current.host}]" if ":" in current.host else current.host
    harness = f"http://{host}:{current.port}/{name}"
    _echo_success(
        f"New provider {name} added to {companion} at {harness}.",
        f"Restart dusha proxy {companion} and dusha serve {companion} to use it.",
    )


def _custom(path: Path, profile: str | None) -> None:
    name = profile or click.prompt(_ask("Profile name"), err=True)
    _replaceable(path, name)
    raw: dict[str, Any] = {"base_url": click.prompt(_ask("Base URL"), value_proc=_upstream, err=True)}
    for kind in sorted(providers.PROTOCOLS):
        typed = click.prompt(_ask(f"{kind} endpoint, Enter for the base URL"), default="", show_default=False,
                             value_proc=_optional_url(kind), err=True)
        if typed:
            raw.setdefault("endpoints", {})[kind] = typed
    raw["auth"] = click.prompt(_ask("Auth header"), type=click.Choice(sorted(providers.AUTH_STYLES)),
                               default="bearer", err=True)
    key = click.prompt(_ask("API key, or {env:NAME}, Enter for none"), default="", show_default=False,
                       hide_input=sys.stdin.isatty(), err=True)
    if key:
        raw["key"] = key
    headers = {}
    while header := click.prompt(
        _ask("Header name, Enter to finish"), default="", show_default=False, err=True
    ):
        headers[header] = click.prompt(_ask(f"Value for {header}"), err=True)
    if headers:
        raw["headers"] = headers
    try:
        providers.provider(name, raw)
    except ValueError as error:
        raise click.ClickException(_sentence(str(error))) from error
    _save(path, name, raw)


def _upstream(value: str) -> str:
    try:
        return http_url(value.strip(), "the base URL")
    except ValueError as error:
        raise click.BadParameter(_sentence(str(error))) from error


def _optional_url(kind: str) -> Callable[[str], str]:
    def check(value: str) -> str:
        if not value.strip():
            return ""
        try:
            return http_url(value.strip(), f"the {kind} endpoint")
        except ValueError as error:
            raise click.BadParameter(_sentence(str(error))) from error

    return check


def _remove(path: Path, profile: str) -> None:
    if profile not in _config(lambda: settings.load(path, environ=False)).providers:
        raise click.ClickException(f"No provider named {profile!r}.")
    _config(lambda: settings.save_provider(path, profile, None))
    _echo_success(f"Removed {profile} from {path.parent.name}.")


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


def _provider_menu(path: Path, *, profile: str | None, refresh: bool) -> None:
    import questionary

    cfg = _config(lambda: settings.load(path, environ=False))
    choices = [
        questionary.Choice("Add a provider", value="add"),
        questionary.Choice("List providers", value="list"),
    ]
    if cfg.providers:
        choices.append(questionary.Choice("Remove a provider", value="remove"))
    action = questionary.select(
        f"Providers for {path.parent.name}", choices=choices, style=_prompt_style()
    ).unsafe_ask()
    if action == "add":
        _add(path, None, profile, refresh)
    elif action == "list":
        _list(path)
    elif action == "remove":
        target = questionary.select(
            "Provider to remove", choices=sorted(cfg.providers), style=_prompt_style()
        ).unsafe_ask()
        _remove(path, target)


@main.command("provider", help="Configure model providers")
@click.argument("name", required=False)
@click.option("--list", "list_only", is_flag=True, help="List configured providers, keys masked.")
@click.option(
    "--add", "add_provider", default=None, metavar="PROVIDER",
    help="Add a provider from the catalog, or custom.",
)
@click.option(
    "--remove", "remove_profile", default=None, metavar="PROFILE", help="Remove a configured provider."
)
@click.option("--profile", help="Profile name for --add, defaults to the provider id.")
@click.option("--refresh", is_flag=True, help="Refetch the models.dev catalog.")
@click.pass_context
def provider_cmd(
    ctx: click.Context,
    name: str | None,
    list_only: bool,
    add_provider: str | None,
    remove_profile: str | None,
    profile: str | None,
    refresh: bool,
) -> None:
    actions = int(list_only) + int(add_provider is not None) + int(remove_profile is not None)
    if actions > 1:
        raise click.UsageError("Use only one of --list, --add, or --remove.")
    if (profile is not None or refresh) and (list_only or remove_profile is not None):
        raise click.UsageError("Options --profile and --refresh only apply to --add or the menu.")
    if add_provider is not None and not add_provider.strip():
        raise click.UsageError("Specify a provider id or custom with --add.")
    if remove_profile is not None and not remove_profile.strip():
        raise click.UsageError("Specify a profile name with --remove.")
    path = _provider_file(ctx, name)
    if list_only:
        _list(path)
    elif add_provider is not None:
        _add(path, add_provider.strip(), profile, refresh)
    elif remove_profile is not None:
        _remove(path, remove_profile.strip())
    elif sys.stdin.isatty():
        _provider_menu(path, profile=profile, refresh=refresh)
    else:
        raise ConfigError("Specify --list, --add <provider>, or --remove <profile>.")


# Deprecated dusha proxy <name> auth, removed in 2.0.21.
@proxy.group("auth", hidden=True)
def auth() -> None:
    _echo_warn("Command dusha proxy auth is deprecated and goes away in 2.0.21.", "Use dusha provider.")


@auth.command("login")
@click.argument("provider", required=False)
@click.option("--profile")
@click.option("--refresh", is_flag=True)
@click.pass_context
def login(ctx: click.Context, provider: str | None, profile: str | None, refresh: bool) -> None:
    _add(_proxy_file(ctx), provider, profile, refresh)


@auth.command("logout")
@click.argument("profile")
@click.pass_context
def logout(ctx: click.Context, profile: str) -> None:
    _remove(_proxy_file(ctx), profile)


@auth.command("status")
@click.pass_context
def status(ctx: click.Context) -> None:
    _list(_proxy_file(ctx))


@main.group("companion", help="Manage companions")
def companion_group() -> None:
    pass


@companion_group.command("add", help="Add a companion")
@click.argument("name", required=False)
@click.option("--serve", "serve_part", is_flag=True, help="Set up only the server keys.")
@click.option("--proxy", "proxy_part", is_flag=True, help="Set up only the proxy keys.")
@click.option("--server-url", default="", help="Server URL for the proxy, skips the prompt.")
@click.option("--token", default="", help="Server token for --server-url.")
@click.pass_context
def companion_add(
    ctx: click.Context, name: str | None, serve_part: bool, proxy_part: bool, server_url: str, token: str
) -> None:
    _configure_companion(
        ctx,
        name,
        serve_part=serve_part,
        proxy_part=proxy_part,
        server_url=server_url,
        token=token,
        command="companion add",
        mode="add",
    )


@companion_group.command("update", help="Update companion configuration")
@click.argument("name", required=False)
@click.option("--serve", "serve_part", is_flag=True, help="Set up only the server keys.")
@click.option("--proxy", "proxy_part", is_flag=True, help="Set up only the proxy keys.")
@click.option("--server-url", default="", help="Server URL for the proxy, skips the prompt.")
@click.option("--token", default="", help="Server token for --server-url.")
@click.pass_context
def companion_update(
    ctx: click.Context, name: str | None, serve_part: bool, proxy_part: bool, server_url: str, token: str
) -> None:
    _configure_companion(
        ctx,
        name,
        serve_part=serve_part,
        proxy_part=proxy_part,
        server_url=server_url,
        token=token,
        command="companion update",
        mode="update",
    )


@companion_group.command("remove", help="Remove a companion")
@click.argument("name", required=False)
@click.option("-y", "--yes", is_flag=True, help="Remove without asking for confirmation.")
@click.pass_context
def companion_remove(ctx: click.Context, name: str | None, yes: bool) -> None:
    home, companion, _ = _companion_home(ctx, name, "companion remove")
    path = home / homes.CONFIG_FILE
    if not path.exists():
        hint = f"Run dusha companion add {companion} first." if companion else ""
        _config(lambda: homes.config_file(path), hint=hint)
    label = companion or home.name
    if not yes:
        if not sys.stdin.isatty():
            raise ConfigError("Pass --yes to remove a companion without a terminal.")
        click.confirm(
            _ask(f"Remove companion {label} and its data at {home}?"), default=False, abort=True, err=True
        )
    shutil.rmtree(home)
    _echo_success(f"Removed companion {label} from {home}.")


@companion_group.command("list", help="List companions")
def companion_list() -> None:
    _print({"root": str(homes.config_root()), "companions": homes.list_companions()})


@main.group("plugin", help="Inspect companion plugins")
def plugin_group() -> None:
    pass


@plugin_group.command("list", help="List plugin states")
@click.argument("name", required=False)
@click.pass_context
def plugin_list(ctx: click.Context, name: str | None) -> None:
    connection, _ = _client(ctx, name)
    _print(_remote(remote.plugins, connection))


if __name__ == "__main__":
    main(prog_name="dusha")

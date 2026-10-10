"""Pluggable CV layouts (PDF renderers): interface, registry and discovery.

This module is the public extension point of the CV generator.  A *layout*
turns a validated :class:`~cv_generator.schema.CVData` into PDF bytes.  Three
routes provide one:

``templates/``
    A *template* is a directory with ``layout.py`` (the drawing code, exposing
    ``LAYOUT``) and ``template.toml`` (its pure values).  The shipped templates
    live in ``<project>/templates/``; personal ones live in the directories
    named by ``cvgen.toml`` ``template_dirs``, which are scanned **first** so a
    personal template can replace a shipped one of the same name.  See
    :func:`discover_templates` and :func:`list_templates`.
``cv_generator.layouts`` entry points
    Any installed distribution may advertise layouts; those are found *after*
    the templates, so a template name always wins over a distribution that
    claims it.
``register_layout``
    Direct registration by library users and tests.

Template discovery is a directory scan on purpose: entry-point metadata is
environment-wide and only exists once the package is installed, so a shipped
template must never depend on it.

The contract
------------
A layout provides three things:

``name``
    The CLI selector used by ``cvgen render <input> --layout <name>``.  It must
    match ``[A-Za-z0-9][A-Za-z0-9._-]*`` and be unique in the registry.
    ``classic`` is taken by the shipped template; do not reuse it.
``description``
    One short human-readable line, shown by ``cvgen layouts``.
``render(cv, options) -> bytes``
    Return the PDF bytes for the validated ``cv``.  ``options`` is a
    :class:`LayoutOptions`; a layout that cannot draw photos simply ignores
    it.  When the content does not fit the layout's page, raise
    :class:`LayoutOverflowError` (or a subclass): the CLI reports it as a
    clean error and writes nothing, so a caller never receives a clipped or
    half-written PDF.  Layouts must not print to stdout - the CLI owns stdout.

Nothing else is required: there is no base class to inherit, only this
protocol.  The implementation may be any PDF engine (ReportLab, fpdf2,
WeasyPrint, a LaTeX subprocess, ...) as long as it honours the contract.

Writing your own layout or template
------------------------------------
The shortest route is a template directory (copy ``templates/minimal`` in this
project).  The layout itself is only a protocol::

    # my_cv_layout/__init__.py
    from cv_generator.layouts import LayoutOptions, LayoutOverflowError
    from cv_generator.schema import CVData


    class InlineLayout:
        name = "inline"
        description = "Dense one-column layout without a photo sidebar."

        def render(self, cv: CVData, options: LayoutOptions) -> bytes:
            pdf = build_my_pdf(cv)          # your own renderer
            if pdf is None:                 # does not fit one page
                raise LayoutOverflowError("experience", 0.0, 0.0, hint="too long")
            return pdf


    LAYOUT = InlineLayout()                 # instance, or a zero-arg factory

and in the providing distribution's ``pyproject.toml``::

    [project.entry-points."cv_generator.layouts"]
    inline = "my_cv_layout:LAYOUT"

After ``pip install my_cv_layout`` the layout is listed by ``cvgen layouts``
and selectable with ``cvgen render cv.json --layout inline``.  A broken entry
point is skipped with a warning; it never crashes the CLI.

Alternatively, drop the same class into ``<dir>/layout.py`` as ``LAYOUT`` and
add ``<dir>`` to ``cvgen.toml`` ``template_dirs``: template directories are
scanned first, so a template may replace a shipped name on purpose.

Registry rules (explicit, no magic)
-----------------------------------
* Registration is by name; a name is registered at most once.
* Re-registering an existing name raises :class:`LayoutRegistrationError`
  unless the caller passes ``override=True`` (the deliberate opt-in, used by
  tools that replace a layout on purpose).
* Templates are registered in scan order (personal directories first, then the
  shipped ones), so a template that claims a name already taken is skipped with
  a warning.  Entry-point layouts are discovered last, so a template always
  wins over a distribution that claims the same name.
* ``available_layouts()`` returns the registry sorted by name, without
  duplicates.
* Template and entry-point discovery both run lazily on first use and are
  idempotent; pass ``force=True`` to re-scan.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from . import paths
from .schema import CVData, PhotoCrop
from .template import LAYOUT_FILE, TEMPLATE_FILE, TemplateError, read_template_toml

__all__ = [
    "DEFAULT_LAYOUT",
    "ENTRY_POINT_GROUP",
    "Layout",
    "LayoutError",
    "LayoutOptions",
    "LayoutOverflowError",
    "LayoutRegistrationError",
    "RegisteredLayout",
    "UnknownLayoutError",
    "available_layouts",
    "discover_layouts",
    "discover_templates",
    "discovery_warnings",
    "get_layout",
    "get_layout_info",
    "list_templates",
    "register_layout",
    "render_layout",
    "reset_layouts",
    "template_search_dirs",
    "unregister_layout",
]

#: Entry-point group that advertises third-party layouts.
#: ``[project.entry-points."cv_generator.layouts"]``
ENTRY_POINT_GROUP = "cv_generator.layouts"

#: Name of the shipped template.  It is *not* a silent fallback: a template
#: must be selectable unambiguously (one template in scope, ``--layout``, or
#: ``cvgen.toml`` ``[template] name``).  The name is used to mark the shipped
#: template in ``cvgen layouts`` and in the error message that lists the
#: available templates.
DEFAULT_LAYOUT = "classic"

#: Origin prefix of a layout that came from a template directory.
TEMPLATE_ORIGIN_PREFIX = "template"

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class LayoutError(ValueError):
    """Base class for layout contract and registry errors."""


class LayoutOverflowError(LayoutError):
    """Raised when content cannot fit a layout's page.

    ``region`` names the overflowing part (``experience``, ``contacts``, ...),
    ``needed`` is the content position in points and ``limit`` where the
    available space ends.  Both use top-down coordinates for the built-in
    layout; a third-party layout may report its own units, but must raise this
    type so the CLI can report the failure cleanly.
    """

    def __init__(self, region: str, needed: float, limit: float, hint: str = ""):
        self.region = region
        self.needed = needed
        self.limit = limit
        msg = (
            f"{region} overflows the one-page layout: content reaches "
            f"{needed:.1f}pt but the available space ends at {limit:.1f}pt "
            f"({needed - limit:.1f}pt over)."
        )
        if hint:
            msg += f" {hint}"
        super().__init__(msg)


class LayoutRegistrationError(LayoutError):
    """Raised when a layout cannot be registered (bad contract, duplicate name)."""


class UnknownLayoutError(LayoutError):
    """Raised when a layout name is not in the registry."""

    def __init__(self, name: str, known: list[str]):
        self.name = name
        self.known = list(known)
        known_text = ", ".join(self.known) if self.known else "(none registered)"
        super().__init__(
            f"unknown layout {name!r}; known layouts: {known_text} "
            f"(run 'cvgen layouts' for descriptions)"
        )


@dataclass(frozen=True)
class LayoutOptions:
    """Explicit, layout-agnostic render options.

    Optional capabilities are opt-in instead of implicit:

    ``photo_path``
        Overrides ``cv.photo`` when given.
    ``photo_crop``
        Overrides ``cv.photo_crop`` when given.

    A layout that cannot draw photos ignores both fields.  Use
    :meth:`photo_for` to get the effective reference the same way the built-in
    layout does.
    """

    photo_path: str | None = None
    photo_crop: PhotoCrop | None = None

    def photo_for(self, cv: CVData) -> tuple[Path | None, PhotoCrop | None]:
        """Return the effective ``(path, crop)`` for ``cv``.

        The options win over the CV data.  A relative name is looked up in the
        private profile's ``photos/`` directory first, then in the shipped
        ``fixtures/`` directory - the sentinel photos the public fixtures name
        (a public clone has no ``profile/`` at all, and the profile always wins
        when it holds a file of that name).  Returns ``(None, None)`` when
        neither the options nor the CV name a photo; the caller (not this
        method) decides whether a missing photo is an error.
        """
        name = self.photo_path or cv.photo
        if not name:
            return None, None
        path = Path(name)
        if not path.is_absolute():
            path = paths.PROFILE_PIC_DIR / path
            if not path.exists():
                shipped = paths.SHIPPED_FIXTURES_DIR / Path(name).name
                if shipped.exists():
                    path = shipped
        crop = self.photo_crop if self.photo_crop is not None else cv.photo_crop
        return path, crop


@runtime_checkable
class Layout(Protocol):
    """Structural contract every layout implements.  No inheritance required."""

    #: Unique registry name, also the CLI selector (``--layout <name>``).
    name: str
    #: One short human-readable line for ``cvgen layouts``.
    description: str

    def render(self, cv: CVData, options: LayoutOptions) -> bytes:
        """Return PDF bytes for ``cv``; raise ``LayoutOverflowError`` if it does not fit."""
        ...  # pragma: no cover - protocol declaration


@dataclass(frozen=True)
class RegisteredLayout:
    """A registry entry: the layout plus where it came from.

    ``path`` is the template directory a template layout was loaded from
    (``None`` for entry points and manual registrations).
    """

    name: str
    description: str
    origin: str
    layout: Layout
    entry_point: str | None = None
    path: Path | None = None


_REGISTRY: dict[str, RegisteredLayout] = {}
_TEMPLATES_LOADED = False
_DISCOVERY_DONE = False
_DISCOVERY_WARNINGS: list[str] = []


def _warn(message: str) -> None:
    _DISCOVERY_WARNINGS.append(message)
    warnings.warn(f"cv_generator: {message}", RuntimeWarning, stacklevel=2)


def _validate_layout(layout: object) -> tuple[str, str]:
    """Return ``(name, description)`` or raise :class:`LayoutRegistrationError`."""
    name = getattr(layout, "name", None)
    description = getattr(layout, "description", None)
    label = f"{type(layout).__module__}.{type(layout).__qualname__}"
    if not isinstance(name, str) or not name:
        raise LayoutRegistrationError(f"layout {label} must define a non-empty 'name'")
    if not _NAME_RE.match(name):
        raise LayoutRegistrationError(
            f"layout name {name!r} is invalid; allowed: letters, digits, '.', '_', '-' "
            f"(must start with a letter or digit)"
        )
    if not isinstance(description, str) or not description.strip():
        raise LayoutRegistrationError(f"layout {name!r} must define a non-empty 'description'")
    if not callable(getattr(layout, "render", None)):
        raise LayoutRegistrationError(f"layout {name!r} must define a callable render(cv, options)")
    return name, description


def register_layout(
    layout: Layout,
    *,
    origin: str = "manual registration",
    entry_point: str | None = None,
    path: Path | None = None,
    override: bool = False,
) -> RegisteredLayout:
    """Register ``layout`` under ``layout.name`` and return its registry entry.

    ``origin`` is the short provenance string shown by ``cvgen layouts``
    (``template <name>``, ``distribution <name> (entry point '<name>')``, ...);
    ``path`` is the template directory the layout was loaded from.
    Registering a name twice raises :class:`LayoutRegistrationError` unless
    ``override=True`` replaces the existing entry.
    """
    name, description = _validate_layout(layout)
    if name in _REGISTRY and not override:
        existing = _REGISTRY[name]
        raise LayoutRegistrationError(
            f"layout name {name!r} is already registered by {existing.origin} "
            f"({existing.description}); pass override=True to replace it"
        )
    info = RegisteredLayout(
        name=name,
        description=description,
        origin=origin,
        layout=layout,
        entry_point=entry_point,
        path=Path(path) if path is not None else None,
    )
    _REGISTRY[name] = info
    return info


def unregister_layout(name: str, *, missing_ok: bool = False) -> RegisteredLayout | None:
    """Remove ``name`` from the registry (test and tooling helper)."""
    info = _REGISTRY.pop(name, None)
    if info is None and not missing_ok:
        raise UnknownLayoutError(name, sorted(_REGISTRY))
    return info


def reset_layouts(*, keep_builtins: bool = True) -> None:
    """Clear the registry and the discovery caches (test and tooling helper).

    With ``keep_builtins`` (the default) the template layouts stay registered
    (re-scanned from their directories); otherwise the next lookup re-scans.
    """
    global _DISCOVERY_DONE, _TEMPLATES_LOADED
    _REGISTRY.clear()
    _TEMPLATES_LOADED = False
    _DISCOVERY_DONE = False
    _DISCOVERY_WARNINGS.clear()
    if keep_builtins:
        _ensure_templates()


# --------------------------------------------------------------------------
# Template discovery: the shipped and personal template directories.
# --------------------------------------------------------------------------
def template_search_dirs() -> list[Path]:
    """Template directories in scan order: **personal first, shipped last**.

    Personal directories come from ``cvgen.toml`` ``template_dirs`` (in the
    order they are written); the shipped ``<project>/templates`` directory is
    always scanned last, so a personal template may replace a shipped one of
    the same name.
    """
    personal = paths.configured_template_dirs()
    shipped = paths.TEMPLATES_DIR
    return [*personal, shipped]


def _load_template_module(layout_file: Path):
    """Import one ``layout.py`` under a unique private module name.

    The template module is a top-level module: it imports the package by its
    absolute name (``from cv_generator.layouts import ...``), which is what a
    template in a colleague's own directory tree must do as well.
    """
    module_name = f"_cv_generator_template_{layout_file.parent.name}_{abs(hash(layout_file))}"
    spec = importlib.util.spec_from_file_location(module_name, layout_file)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise TemplateError(f"cannot load template module {layout_file}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _template_declaration(template_dir: Path) -> tuple[str | None, str | None]:
    """``(name, description)`` from ``template.toml``; ``(None, None)`` if absent."""
    toml_path = template_dir / TEMPLATE_FILE
    if not toml_path.is_file():
        return None, None
    values = read_template_toml(toml_path)
    name = values.get("name")
    description = values.get("description")
    return (str(name) if name else None, str(description) if description else None)


def discover_templates(dirs: list[Path] | None = None, *, force: bool = False) -> list[str]:
    """Scan every template directory and register the layouts found there.

    ``dirs`` defaults to :func:`template_search_dirs` (personal directories
    first, the shipped ``templates/`` last); only that default scan is memoised,
    an explicit ``dirs`` argument always scans now.  Each immediate sub-directory
    is a candidate template; it must contain ``layout.py``, which must expose
    ``LAYOUT`` (a layout instance, or a zero-argument factory/class returning
    one).  Returns the names registered by this call.

    A candidate is skipped with a warning - never a crash - when it has no
    ``layout.py``, when the module cannot be imported, when ``LAYOUT`` does not
    honour the contract, or when its name is already registered (the first
    registration wins: a personal template beats the shipped one).
    """
    global _TEMPLATES_LOADED
    # Only the default scan is memoised; an explicit ``dirs`` argument is a
    # direct request to scan those directories now.
    default_scan = dirs is None
    if default_scan and _TEMPLATES_LOADED and not force:
        return []
    if default_scan:
        _TEMPLATES_LOADED = True

    registered: list[str] = []
    for directory in dirs if dirs is not None else template_search_dirs():
        directory = Path(directory)
        if not directory.is_dir():
            continue
        for template_dir in sorted(directory.iterdir(), key=lambda p: p.name):
            if not template_dir.is_dir() or template_dir.name.startswith("."):
                continue
            layout_file = template_dir / LAYOUT_FILE
            if not layout_file.is_file():
                _warn(f"template directory {template_dir} has no {LAYOUT_FILE}; skipped")
                continue
            try:
                module = _load_template_module(layout_file)
                layout = _instantiate(getattr(module, "LAYOUT", None))
                if layout is None:
                    raise TemplateError(f"{layout_file} defines no LAYOUT")
                name = _validate_layout(layout)[0]
                declared, _description = _template_declaration(template_dir)
                if declared is not None and declared != name:
                    _warn(
                        f"template {template_dir}: {TEMPLATE_FILE} says name={declared!r} "
                        f"but {LAYOUT_FILE} registers {name!r}; using {name!r}"
                    )
                existing = _REGISTRY.get(name)
                same_object = existing is not None and existing.layout is layout
                # A template normally registers itself when its module is
                # imported; that entry only needs its origin and directory
                # completed here.  Anything else is a real name clash and is
                # skipped below with a warning (the first registration wins,
                # and personal directories are scanned before the shipped one).
                info = register_layout(
                    layout,
                    origin=f"{TEMPLATE_ORIGIN_PREFIX} {name}",
                    path=template_dir,
                    override=same_object,
                )
            except Exception as exc:
                _warn(f"skipping template {template_dir}: {exc}")
                continue
            registered.append(info.name)
    return registered


def _ensure_templates() -> None:
    """Run the template scan once (the first registry lookup triggers it)."""
    discover_templates()


def list_templates() -> list[tuple[str, str, str, Path]]:
    """The template layouts as ``(name, description, origin, path)``, by name.

    Only layouts that came from a template directory are listed; a layout
    installed as a distribution entry point or registered manually is not a
    template and does not appear here.
    """
    _ensure_templates()
    discover_layouts()
    return [
        (info.name, info.description, info.origin, info.path)
        for info in sorted(_REGISTRY.values(), key=lambda entry: entry.name)
        if info.path is not None
    ]


def _distribution_name(entry_point) -> str:
    dist = getattr(entry_point, "dist", None)
    name = getattr(dist, "name", None)
    if not name:
        metadata = getattr(dist, "metadata", None)
        name = metadata.get("Name") if metadata else None
    return name or "unknown distribution"


def _instantiate(obj):
    """Accept a layout instance or a zero-argument factory/class returning one."""
    if isinstance(obj, type) or (callable(obj) and not hasattr(obj, "render")):
        return obj()
    return obj


def discover_layouts(*, force: bool = False) -> list[str]:
    """Register the layouts advertised by installed distributions.

    Reads the ``cv_generator.layouts`` entry-point group
    (:data:`ENTRY_POINT_GROUP`) and returns the names registered by this call.
    The scan runs once; pass ``force=True`` to scan again.

    A broken or conflicting entry point is skipped with a warning (see
    :func:`discovery_warnings`) and never raises, so a third-party packaging
    mistake cannot break the CLI.
    """
    global _DISCOVERY_DONE
    _ensure_templates()
    if _DISCOVERY_DONE and not force:
        return []
    _DISCOVERY_DONE = True

    try:
        entry_points = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # pragma: no cover - defective metadata on the host
        _warn(f"layout discovery unavailable: {exc}")
        return []

    registered: list[str] = []
    for entry_point in entry_points:
        distribution = _distribution_name(entry_point)
        label = f"distribution {distribution} (entry point {entry_point.name!r})"
        try:
            layout = _instantiate(entry_point.load())
            info = register_layout(
                layout,
                origin=label,
                entry_point=f"{ENTRY_POINT_GROUP}:{entry_point.name}",
            )
        except Exception as exc:
            _warn(f"skipping layout entry point {entry_point.name!r}: {exc}")
            continue
        registered.append(info.name)
    return registered


def discovery_warnings() -> list[str]:
    """Warnings collected by the last :func:`discover_layouts` call."""
    return list(_DISCOVERY_WARNINGS)


def available_layouts() -> list[RegisteredLayout]:
    """All registered layouts, sorted by name, without duplicates."""
    _ensure_templates()
    discover_layouts()
    return sorted(_REGISTRY.values(), key=lambda info: info.name)


def get_layout_info(name: str) -> RegisteredLayout:
    """Registry entry for ``name``; raises :class:`UnknownLayoutError`."""
    _ensure_templates()
    discover_layouts()
    info = _REGISTRY.get(name)
    if info is None:
        raise UnknownLayoutError(name, sorted(_REGISTRY))
    return info


def get_layout(name: str) -> Layout:
    """The layout registered as ``name``; raises :class:`UnknownLayoutError`."""
    return get_layout_info(name).layout


def render_layout(name: str, cv: CVData, options: LayoutOptions | None = None) -> bytes:
    """Render ``cv`` with the layout called ``name`` (``options`` defaults to none).

    Raises :class:`UnknownLayoutError` for an unknown name and
    :class:`LayoutError` when the layout does not return bytes.  Overflow and
    photo errors come from the layout itself.
    """
    layout = get_layout(name)
    result = layout.render(cv, options if options is not None else LayoutOptions())
    if not isinstance(result, (bytes, bytearray)):
        raise LayoutError(
            f"layout {name!r} returned {type(result).__name__}, expected PDF bytes"
        )
    return bytes(result)

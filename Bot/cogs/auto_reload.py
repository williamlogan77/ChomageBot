import glob
import importlib
import logging
import os
import pathlib
import re
import sys

from discord.ext import commands, tasks

log = logging.getLogger(__name__)

COG_DIR = "./cogs"
UTILS_DIR = "./utils"
POLL_SECONDS = 30

# Reloading ourselves mid-loop would cancel this very watch task, so the
# utils-triggered "reload every cog" pass skips this extension.
_SELF_EXT = "cogs.auto_reload"


class AutoReload(commands.Cog):
    """Watches cog AND utils files for changes and hot-reloads in-process.

    Pairs with scripts/deploy.sh: cron pulls latest main, mtimes shift,
    this cog reloads the affected code in-process. No restart, no signals,
    no external IPC. The bot's Discord connection stays open.

    - ``cogs/*.py`` changed  → ``reload_extension`` for that cog.
    - ``utils/*.py`` changed → ``importlib.reload`` that module, THEN reload
      every loaded cog. The second step is required because
      ``reload_extension`` does NOT reload imported submodules, and cog
      module-level constants bake in references to utils objects at import
      time (e.g. ``cogs.match_analysis.CHART_DEFS`` holds
      ``utils.match_analysis.plot_*`` functions). Without it, ``utils``
      edits silently needed a full bot restart to take effect.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._mtimes: dict[str, float] = {}
        self.watch.start()

    def cog_unload(self):
        self.watch.cancel()

    @tasks.loop(seconds=POLL_SECONDS)
    async def watch(self) -> None:
        # 1. utils/*.py — reload changed modules, then reload all cogs so
        #    their import-time bindings pick up the new code. If a module
        #    reload fails we do NOT go on to reload cogs: importlib.reload can
        #    leave a module half-executed, and reloading cogs against that
        #    would run their setup/cog_unload against inconsistent code.
        changed_utils = self._detect_changed(UTILS_DIR)
        if changed_utils:
            if self._reload_modules(changed_utils):
                await self._reload_all_cogs(reason="utils change")
                # We just reloaded every loaded cog; refresh the mtime baseline
                # for cogs we already track so the per-cog pass below doesn't
                # reload them a second time. (New, never-seen cog files are
                # left untouched so step 2 still loads them.)
                for path in glob.glob(f"{COG_DIR}/*.py"):
                    if path in self._mtimes:
                        try:
                            self._mtimes[path] = os.path.getmtime(path)
                        except OSError:
                            pass
            else:
                log.error(
                    "AutoReload: a utils reload failed — skipping the cog "
                    "reload so cogs don't bind against half-reloaded modules. "
                    "Fix the util module; the next change will retry."
                )

        # 2. cogs/*.py — reload (or first-time load) changed cogs.
        for path in sorted(glob.glob(f"{COG_DIR}/*.py")):
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            ext_name = f"cogs.{os.path.basename(path)[:-3]}"
            prev = self._mtimes.get(path)

            if prev is None:
                # First time we've seen this file. If the bot already has the
                # extension loaded (initial discovery in main.py), just record
                # mtime. Otherwise a new cog was added — load it.
                if ext_name not in self.bot.extensions:
                    try:
                        await self.bot.load_extension(ext_name)
                        log.info(f"AutoReload: loaded {ext_name}")
                    except Exception as exc:
                        log.error(f"AutoReload: failed to load {ext_name}: {exc}")
                self._mtimes[path] = mtime
            elif mtime != prev:
                try:
                    await self.bot.reload_extension(ext_name)
                    log.info(f"AutoReload: reloaded {ext_name}")
                except Exception as exc:
                    log.error(f"AutoReload: failed to reload {ext_name}: {exc}")
                self._mtimes[path] = mtime

    def _detect_changed(self, directory: str) -> list[str]:
        """Paths under ``directory`` whose mtime changed since last poll.

        Updates the baseline as it goes. First sight of a file records its
        mtime WITHOUT flagging it, so a restart doesn't reload everything on
        the first tick.
        """
        changed: list[str] = []
        for path in sorted(glob.glob(f"{directory}/*.py")):
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            prev = self._mtimes.get(path)
            if prev is None:
                self._mtimes[path] = mtime
            elif mtime != prev:
                changed.append(path)
                self._mtimes[path] = mtime
        return changed

    def _reload_modules(self, paths: list[str]) -> bool:
        """Reload changed utils modules in place, plus any loaded utils module
        that imports one of them.

        The dependent reload is what fixes stale bindings: a module that did
        ``from utils.changed import foo`` keeps the OLD ``foo`` until it is
        itself re-executed, so we reload those dependents after the modules
        they import. Changed modules go first so dependents re-bind against
        fresh code. Returns True only if every reload succeeded; the caller
        uses that to decide whether it's safe to reload cogs.

        We do NOT blanket-reload every ``utils.*`` module — only the changed
        ones and their importers — so an edit to one util doesn't re-run
        unrelated module-level state (e.g. reset riot_client's rate limiter).
        This resolves the codebase's depth-1 utils graph (riot_stats imports
        riot_client) in one pass; any deeper chain settles on the next poll.
        """
        changed = {f"utils.{os.path.basename(p)[:-3]}" for p in paths}
        ok = True

        def _reload(name: str) -> None:
            nonlocal ok
            mod = sys.modules.get(name)
            if mod is None:
                # Not imported yet (or under another name) — a later cog
                # reload will import the fresh version anyway.
                return
            try:
                importlib.reload(mod)
                log.info(f"AutoReload: reloaded module {name}")
            except Exception as exc:
                log.error(f"AutoReload: failed to reload module {name}: {exc}")
                ok = False

        # 1. The changed modules themselves.
        for name in sorted(changed):
            _reload(name)
        # 2. Loaded utils modules that import a changed one, so their
        #    from-imports re-point to the reloaded objects.
        for name, mod in list(sys.modules.items()):
            if (
                name.startswith("utils.")
                and name not in changed
                and self._imports_any(mod, changed)
            ):
                _reload(name)
        return ok

    @staticmethod
    def _imports_any(mod, module_names: set[str]) -> bool:
        """True if ``mod``'s source imports any of ``module_names`` (matches
        both ``import utils.x`` and ``from utils.x import ...``)."""
        path = getattr(mod, "__file__", None)
        if not path:
            return False
        try:
            src = pathlib.Path(path).read_text(encoding="utf-8")
        except OSError:
            return False
        return any(
            re.search(rf"(?:from|import)\s+{re.escape(name)}\b", src) for name in module_names
        )

    async def _reload_all_cogs(self, *, reason: str) -> None:
        """Reload every loaded cog except ourselves. On failure discord.py
        rolls the extension back to its previous version, so a broken edit
        can't leave a cog unloaded."""
        for ext_name in list(self.bot.extensions):
            if ext_name == _SELF_EXT:
                continue
            try:
                await self.bot.reload_extension(ext_name)
                log.info(f"AutoReload: reloaded {ext_name} ({reason})")
            except Exception as exc:
                log.error(f"AutoReload: failed to reload {ext_name}: {exc}")

    @watch.before_loop
    async def before_watch(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoReload(bot))

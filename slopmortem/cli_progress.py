"""Rich progress display shared by ingest, query, and the eval recorders."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Self, override

from rich.console import Console
from rich.measure import Measurement
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    Task,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.segment import Segment
from rich.text import Text

if TYPE_CHECKING:
    from types import TracebackType

    from rich.console import ConsoleOptions, RenderResult
    from rich.progress_bar import ProgressBar


class _StackedBar:
    """Stack one ``ProgressBar`` vertically across N rows.

    Rich yields ``ProgressBar`` segments without a trailing newline, so a
    ``Group`` of N bars renders inline. Explicit ``Segment.line`` between
    copies stacks them.
    """

    def __init__(self, bar: ProgressBar, height: int) -> None:
        self._bar = bar
        self._height = height

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        for i in range(self._height):
            yield from self._bar.__rich_console__(console, options)
            if i < self._height - 1:
                yield Segment.line()

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        return Measurement.get(console, options, self._bar)


class ThickBarColumn(BarColumn):
    """Bar spanning ``height`` terminal rows; adjacent columns auto-pad to the tallest cell."""

    height = 1

    @override
    def render(self, task: Task) -> _StackedBar | Text:  # pyright: ignore[reportIncompatibleMethodOverride]
        if task.total is None or task.total <= 1:
            return Text("")
        return _StackedBar(super().render(task), self.height)


class OptionalMofNCompleteColumn(MofNCompleteColumn):
    """Hide ``M of N`` for single-shot phases; the pulse-then-fill bar already says done vs not."""

    @override
    def render(self, task: Task) -> Text:
        if task.total is None or task.total <= 1:
            return Text("")
        return super().render(task)


class OptionalETAColumn(TimeRemainingColumn):
    """Suppress ETA on finished or single-shot phases.

    Rich keeps painting ``0:00`` on finished tasks (reads as "still
    computing"); single-shot phases have no meaningful remaining-time estimate.
    """

    @override
    def render(self, task: Task) -> Text:
        if task.finished or task.total is None or task.total <= 1:
            return Text("")
        return Text("eta ", style="dim") + super().render(task)


class RichPhaseProgress[PhaseT: StrEnum]:
    """Rich-backed phase progress shared by ingest, query, and recording pipelines.

    Tasks are created lazily so unreached phases don't render empty bars; a
    red error-count badge appends to the description on per-phase failures.
    """

    def __init__(
        self,
        labels: dict[PhaseT, str],
        console: Console | None = None,
    ) -> None:
        self._labels = labels
        self._console = console if console is not None else Console(stderr=True)
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}", justify="left"),
            ThickBarColumn(
                bar_width=40,
                style="grey50",
                complete_style="cyan",
                finished_style="green",
                pulse_style="cyan",
            ),
            OptionalMofNCompleteColumn(),
            TextColumn("[dim]•"),
            TimeElapsedColumn(),
            OptionalETAColumn(),
            console=self._console,
            transient=False,
        )
        self._tasks: dict[PhaseT, TaskID] = {}
        self._phase_errors: dict[PhaseT, int] = {}
        self._phase_status: dict[PhaseT, str] = {}

    def __enter__(self) -> Self:
        self._progress.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._progress.__exit__(exc_type, exc_val, exc_tb)

    @property
    def console(self) -> Console:
        return self._console

    def _label(self, phase: PhaseT) -> str:
        styled = f"[bold cyan]{self._labels[phase]}[/bold cyan]"
        status = self._phase_status.get(phase)
        if status:
            styled = f"{styled} [dim]({status})[/dim]"
        n = self._phase_errors.get(phase, 0)
        if not n:
            return styled
        noun = "error" if n == 1 else "errors"
        return f"{styled} [bold red]({n} {noun})[/bold red]"

    def start_phase(self, phase: PhaseT, total: int | None) -> None:
        """Start or restart a phase task.

        ``total`` ``None`` or ``<= 1`` pulses instead of flashing 0→1;
        ``end_phase`` snaps the bar to filled.
        """
        bar_total = total if total and total > 1 else None
        if phase in self._tasks:
            self._progress.reset(self._tasks[phase], total=bar_total)
            self._progress.update(self._tasks[phase], description=self._label(phase))
            return
        self._tasks[phase] = self._progress.add_task(self._label(phase), total=bar_total)

    def advance_phase(self, phase: PhaseT, n: int = 1) -> None:
        tid = self._tasks.get(phase)
        if tid is not None:
            self._progress.advance(tid, n)

    def end_phase(self, phase: PhaseT) -> None:
        """Snap the bar to filled.

        Indeterminate phases (``total is None``) freeze at
        ``total = max(completed, 1)``; otherwise fill to ``total``.
        """
        tid = self._tasks.get(phase)
        if tid is None:
            return
        task = self._progress.tasks[tid]
        if task.total is None:
            completed = max(task.completed, 1)
            self._progress.update(
                tid,
                total=completed,
                completed=completed,
                description=self._label(phase),
            )
            return
        self._progress.update(
            tid, completed=task.total or task.completed, description=self._label(phase)
        )

    def set_phase_status(self, phase: PhaseT, status: str | None) -> None:
        if status:
            self._phase_status[phase] = status
        else:
            self._phase_status.pop(phase, None)
        tid = self._tasks.get(phase)
        if tid is not None:
            self._progress.update(tid, description=self._label(phase))

    def log(self, message: str) -> None:
        self._console.log(message)

    def error(self, phase: PhaseT, message: str) -> None:
        """Bump the error count for *phase* and log a red line above the bars."""
        self._phase_errors[phase] = self._phase_errors.get(phase, 0) + 1
        tid = self._tasks.get(phase)
        if tid is not None:
            self._progress.update(tid, description=self._label(phase))
        self._console.log(f"[bold red]ERROR[/bold red] [{phase.value}] {message}")

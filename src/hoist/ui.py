from rich.console import Console

console = Console(highlight=False)


def step(msg: str) -> None:
    console.print(f"\n[bold blue]==> {msg}[/]")


def info(msg: str) -> None:
    console.print(msg, markup=False)


def ok(msg: str) -> None:
    console.print(f"[green]{_esc(msg)}[/]")


def warn(msg: str) -> None:
    console.print(f"[bold yellow]WARN:[/] {_esc(msg)}")


def error(msg: str) -> None:
    console.print(f"[bold red]ERROR:[/] {_esc(msg)}")


def _esc(msg: str) -> str:
    return msg.replace("[", "\\[")

from dataclasses import dataclass

from station.intake.receipt import SampleReceipt
from station.intake.repository import PieceConclusion


@dataclass(frozen=True)
class AcceptanceResult:
    conclusion: PieceConclusion
    receipt: SampleReceipt | None

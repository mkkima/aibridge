from dataclasses import dataclass, field
from typing import Dict, Any, Optional
from datetime import datetime

@dataclass
class DebateState:
    topic: str
    normalized_problem: Optional[Dict[str, Any]] = None
    round_1_positions: Dict[str, str] = field(default_factory=dict)
    round_2_critiques: Dict[str, str] = field(default_factory=dict)
    synthesis: Optional[str] = None
    decision_matrix: Optional[str] = None
    final_report_path: Optional[str] = None
    raw_responses: Dict[str, Any] = field(default_factory=dict)
    claims_to_verify: list = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self):
        return {
            "topic": self.topic,
            "normalized_problem": self.normalized_problem,
            "round_1_positions": self.round_1_positions,
            "round_2_critiques": self.round_2_critiques,
            "synthesis": self.synthesis,
            "decision_matrix": self.decision_matrix,
            "final_report_path": self.final_report_path,
            "raw_responses": self.raw_responses,
            "claims_to_verify": self.claims_to_verify,
            "created_at": self.created_at
        }


from dataclasses import dataclass


MAX_ACTIVE_EXTRACTION_JOBS = 1


@dataclass(frozen=True, slots=True)
class ExtractionResourcePolicy:
    max_active_jobs: int = MAX_ACTIVE_EXTRACTION_JOBS

    def __post_init__(self) -> None:
        if self.max_active_jobs != MAX_ACTIVE_EXTRACTION_JOBS:
            raise ValueError("V1 extraction capacity must be exactly 1")

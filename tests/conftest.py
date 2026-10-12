import sys
from pathlib import Path

# Lets tests share fixtures such as test_review.impact_payload.
sys.path.insert(0, str(Path(__file__).parent))

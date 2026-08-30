"""Database models and repositories.

Every model module is imported here so ``Base.metadata`` is always complete:
the tables reference each other by name, and a partial import leaves a
``ForeignKey`` unable to resolve its target table.
"""

from vidgen.db.base import Base

from . import animation_models as animation_models
from . import continuity_models as continuity_models
from . import control_command_models as control_command_models
from . import cost_models as cost_models
from . import episode_analysis_models as episode_analysis_models
from . import final_editorial_models as final_editorial_models
from . import image_generation_models as image_generation_models
from . import models as models
from . import narration_models as narration_models
from . import publication_models as publication_models
from . import render_models as render_models
from . import repair_models as repair_models
from . import review_models as review_models
from . import script_models as script_models
from . import storyboard_models as storyboard_models
from . import subtitle_models as subtitle_models
from . import transcription_models as transcription_models
from . import upload_models as upload_models
from . import visual_qa_models as visual_qa_models
from . import workflow_models as workflow_models

__all__ = ["Base"]

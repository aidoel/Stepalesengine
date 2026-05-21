"""Single source of truth for thresholds and weights.

All tunable parameters live here. Phase 6 will replace hand-tuned values with
sweep-optimised values driven by the held-out confusion matrix.

Naming convention: each constant carries a short comment naming the modules
that consume it so it is obvious where the threshold reaches the runtime.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Score classifier
# Consumed by: manufacturing_pipeline.classification.score_classifier
# ---------------------------------------------------------------------------

# MARGIN_THRESHOLD: minimum (top1 - top2) score gap before tiebreakers fire.
MARGIN_THRESHOLD = 0.15
# CONFIDENCE_THRESHOLD: softmax probability that the winner must clear to be
# emitted as the final label. Anything below it degrades to 'uncertain'.
CONFIDENCE_THRESHOLD = 0.65
# SOFTMAX_TEMPERATURE: < 1.0 sharpens the distribution so dominant signals
# pull confidence above the threshold. Calibrated against the ideal-flat-plate
# / ideal-RHS-profile fixtures so each obvious case clears >= 0.70.
SOFTMAX_TEMPERATURE = 0.5

# ---------------------------------------------------------------------------
# Cross-section constancy + sampling
# Consumed by: manufacturing_pipeline.geometry.cross_section,
#              manufacturing_pipeline.geometry.feature_extractor
# ---------------------------------------------------------------------------

CROSS_SECTION_CV_LIMIT = 0.02
CROSS_SECTION_N_SLICES = 7
# Samples per curved (non-line) edge when discretising a 2D wire polyline.
CROSS_SECTION_CURVE_SAMPLES_PER_EDGE = 16

# ---------------------------------------------------------------------------
# Hollow detection (volumetric)
# Consumed by: manufacturing_pipeline.geometry.feature_extractor
# ---------------------------------------------------------------------------

# Lower bound on bbox_fill that still counts as "could be hollow"; anything
# below this is considered too sparse to be a real solid.
HOLLOW_BBOX_FILL_MIN = 0.05
HOLLOW_BBOX_FILL_MAX = 0.45
SOLID_BBOX_FILL_MIN = 0.60

# ---------------------------------------------------------------------------
# Mesh triangulation deflection
# Consumed by: manufacturing_pipeline.geometry.feature_extractor
# ---------------------------------------------------------------------------

# Default chord deflection used by BRepMesh_IncrementalMesh in the convex-hull
# helper and the mesh fallback. Smaller = finer triangulation, slower.
MESH_DEFLECTION_DEFAULT_MM = 0.5
# Fraction of the bbox diagonal used to derive an adaptive deflection.
MESH_DEFLECTION_DIAG_FRACTION = 0.05
# Hard cap on the adaptive deflection.
MESH_DEFLECTION_MAX_MM = 2.0

# ---------------------------------------------------------------------------
# Unfold probe (sheet-metal)
# Consumed by: manufacturing_pipeline.geometry.unfold_probe
# ---------------------------------------------------------------------------

UNFOLD_K_FACTOR = 0.44
UNFOLD_THICKNESS_CV_LIMIT = 0.05
UNFOLD_MAX_OTHER_FACES_RATIO = 0.05
# Cosine-of-angle ceiling for treating two planar faces as antiparallel
# during thickness probing (dot < UNFOLD_ANTIPARALLEL_DOT_MAX).
UNFOLD_ANTIPARALLEL_DOT_MAX = -0.85
# Cylindrical-area / total-area ratio above which the part is treated as a
# closed body (e.g. a pipe) and reported as cyclic_graph.
UNFOLD_CLOSED_BODY_CYL_AREA_RATIO = 0.6
# Multiplier on the minimum-thickness pair when filtering "sheet-like" pairs.
UNFOLD_SHEET_PAIR_THICKNESS_MULTIPLIER = 3.0
# Minimum angle (degrees) between two planar faces before the bend counts.
UNFOLD_MIN_BEND_ANGLE_DEG = 2.0
# Angle (degrees) above which a bend is considered a hem.
UNFOLD_HEM_ANGLE_MIN_DEG = 170.0
# Coplanar/antiparallel cosine cut-off when picking the two flats joined by a
# bend cylinder group.
UNFOLD_COPLANAR_COS_LIMIT = 0.95
# A real bend's hinge axis lies in the plane of both flanges it joins, so it is
# perpendicular to their normals. A rounded corner's cylinder axis is parallel
# to the flange normal instead. Reject a bend candidate when |axis . flange
# normal| exceeds this (cos 78 deg ~ 0.20).
UNFOLD_BEND_AXIS_PERP_DOT_MAX = 0.20
# A flange (sheet face) pair gap is accepted as the thickness when within this
# relative tolerance of the dominant (largest-area) antiparallel pair's gap.
UNFOLD_FLANGE_GAP_REL_TOL = 0.20
# Material thicker than this (mm) is plate / machining work, not sheet-metal
# bending. A thick flat block has two large parallel faces and would otherwise
# pass the no-bends "flat plate" path as a trivial unfold success.
UNFOLD_MAX_SHEET_THICKNESS_MM = 20.0

# ---------------------------------------------------------------------------
# Profile matcher
# Consumed by: manufacturing_pipeline.geometry.profile_matcher,
#              manufacturing_pipeline.geometry.cross_section
# ---------------------------------------------------------------------------

PROFILE_FILLET_COLLAPSE_RATIO = 0.15  # of min(h, b)
PROFILE_RESIDUAL_TOLERANCE_MM = 1.0
PROFILE_RESIDUAL_REJECT_MM = 1.5
# Profile-match confidence above which feature_merger sets cross_section_constant.
PROFILE_MATCH_CONFIDENCE_THRESHOLD = 0.5
# Per-mm decay scale used to convert residuals to confidence (exp(-r / scale)).
PROFILE_CONFIDENCE_DECAY_MM = 0.5

# Cross-section signature thresholds (consumed by cross_section.compute_signature)
# std-dev/mean radius below this => circle.
CIRCULARITY_STDDEV_RATIO = 0.02
# Vertex turning angle in degrees that counts as a "corner".
TURNING_ANGLE_DEG_MIN = 30.0
# Hausdorff-distance / bbox-diagonal ratio for reflective / point symmetry.
SYMMETRY_HAUSDORFF_RATIO = 0.01

# Fillet-collapse heuristic (consumed by cross_section.collapse_fillets).
FILLET_NOISE_DEG = 0.5
FILLET_VERTEX_DEG_MAX = 20.0
FILLET_RUN_DEG_MIN = 60.0

# ---------------------------------------------------------------------------
# OBB stability fallback
# Consumed by: manufacturing_pipeline.geometry.cross_section,
#              manufacturing_pipeline.geometry.feature_extractor
# ---------------------------------------------------------------------------

OBB_PRINCIPAL_AXIS_MIN_RATIO = 1.2

# ---------------------------------------------------------------------------
# Assembly matcher (manufacturing_pipeline.assembly.matcher)
# Confidence values reported per match strategy. These are not "thresholds"
# but downstream consumers compare against them, so they live here too.
# ---------------------------------------------------------------------------

MATCH_CONFIDENCE_ONE_TO_ONE = 1.0
MATCH_CONFIDENCE_ORDERED = 0.8
MATCH_CONFIDENCE_OCAF = 0.7
MATCH_CONFIDENCE_BY_NAME = 0.4
MATCH_CONFIDENCE_UNMATCHED_SOLID = 0.2

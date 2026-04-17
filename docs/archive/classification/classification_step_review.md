# Classificatie Step Review - STEP 0 (actueel)

## Doel
Dit document beschrijft uitsluitend de huidige, actieve Step 0 criteria en de
beslislogica in:
- `manufacturing_pipeline/analysis/classification.py`
- `manufacturing_pipeline/analysis/step0_section_tools.py`

## Begrippen
- `section`: 2D doorsnede van een solid.
- `core section`: representatieve sectie uit het dominante sectiecluster.
- `holes`: aantal interne lussen in de sectie.
- `reentrant_corners`: aantal concave hoeken in de sectie.
- `dikteConstant`: constante dikte-indicatie op basis van face-analyse.

## Stapoverzicht
1. `0.1` Slice-validatie + massieve ronde-as check
2. `0.2` Gesloten-hol (buis/koker)
3. `0.3` Open profiel (L/U/I/T)
4. `0.4a` Vlakke plaat
5. `0.4b` Gezette plaat
6. `0.5` Massief profiel fallback

Stopregel:
- Eerste match met `fallthrough=False` stopt de Step 0 classificatie.

---

## Stap 0.1 - Slice-validatie

Poortcriteria:
- stabiele extrusie-as gevonden
- minimaal 3 geldige dwarsdoorsneden
- dominant cluster ratio >= `STEP0_CLUSTER_RATIO_MIN` (0.30)

As-selectie (2 fasen):
- fase A: kandidaten met `axis_extent >= 0.60 * max_extent`
- fase B: als fase A geen geldige kandidaat oplevert, fallback naar overige assen

Kwaliteitsgates op kandidaat-as:
- `success >= 3`
- `cluster_ratio >= 0.30`
- `mean_section_distance <= 0.50`
- `area_cv <= 0.25`

Subcheck massieve ronde as (alleen bij ronde massieve kernsectie):
- vereist o.a. `holes == 0`, `reentrant_corners == 0`
- ratio: $A_{axial} / (D_{max} \times L)$
- als ratio < `ROUND_SHAFT_AXIAL_AREA_RATIO_MIN` (0.99) -> `ANDERS`

---

## Stap 0.2 - Gesloten-hol (buis/koker)

Primaire herkenning:
- `holes == 1`
- outer+inner near-circle -> ronde buis pad
- outer+inner near-rectangle -> rechthoekige koker pad

### Ronde holle buis: extra consistentiecheck
Doel: standaard buis onderscheiden van bewerkte/variabele buis.

Check A: buitendiameter-variatie (OD)
- sampleposities langs lengte: `(0.05, 0.25, 0.75, 0.95)`
- per sectie: `OD = max(bbox_w, bbox_h)` van buitencontour
- maat: `(max_OD - min_OD) / max_OD`
- drempel: `> 0.15` -> `ANDERS`

Check B: wanddikte-variatie
- per sectie met zichtbare inner ring:
  - `ID = max(bbox_w, bbox_h)` van inner ring
  - `t = (OD - ID) / 2`
- maat: `(max_t - min_t) / max_t`
- drempel: `> 0.25` -> `ANDERS`

Belangrijke logica bij gaten:
- secties zonder inner ring worden overgeslagen in wanddikte-check
- reden: dit kan komen door radiale gaten of lokale topologie en mag niet
  automatisch tot `ANDERS` leiden
- OD-check blijft robuust omdat radiale gaten de buitenste contour meestal niet
  veranderen

Extra voorwaarde:
- check alleen als `length_ratio >= 1.5` (te korte/stompe buizen -> skip check)

Uitkomst stap 0.2:
- ronde buis + checks geslaagd -> `RONDE_BUIS`
- ronde buis + een check faalt -> `ANDERS`
- rechthoekig + criteria geslaagd -> `RECHTHOEKIGE_KOKER`
- anders -> door naar `0.3`

---

## Stap 0.3 - Open profiel
Criteria:
- `holes == 0`
- `reentrant_corners > 0`
- geen bent-sheet veto
- template match binnen open profielen

Uitkomst:
- match -> `PROFIEL`
- geen match -> door naar `0.4a`

---

## Stap 0.4a - Vlakke plaat
Harde criteria:
- `holes == 0`
- `reentrant_corners == 0`
- `dikteConstant == True`

Confidence-tier:
- high confidence: stop als `near-rectangle` en `bbox_ratio <= 0.30`
- low confidence: `PLAAT` met `fallthrough=True`

---

## Stap 0.4b - Gezette plaat
Criteria:
- `holes == 0`
- `reentrant_corners > 0`
- `dikteConstant == True`

Asfallback:
- bij twijfel op primaire as worden alternatieve assen geëvalueerd

Uitkomst:
- match -> `GEZETTE_PLAAT`
- geen match -> door naar `0.5`

---

## Stap 0.5 - Massief profiel fallback
Dimensionele/ratio fallback voor `PROFIEL` versus `ANDERS`.

## Kernparameters
- `STEP0_CLUSTER_RATIO_MIN = 0.30`
- `ROUND_SHAFT_MIN_LENGTH_RATIO = 3.0` (massieve-as check)
- `ROUND_SHAFT_AXIAL_AREA_RATIO_MIN = 0.99`
- Step 0.2 rond-hol checks:
  - `OD_VAR_MAX = 0.15`
  - `WALL_VAR_MAX = 0.25`
  - `HOLLOW_TUBE_MIN_LENGTH_RATIO = 1.5`

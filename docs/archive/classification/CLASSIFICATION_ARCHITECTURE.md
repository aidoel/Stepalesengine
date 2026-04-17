# Classification Architecture - Current State & Generic Solution

## 1. STEP Parser - Functie en Plaats

### `parse_step_assembly_structure()` - Wat doet het?

**Locatie:** `manufacturing_pipeline/analysis/assembly_analysis.py:635`

**Doel:** Extractie van originele partnamen uit STEP-bestand metadata

```python
def parse_step_assembly_structure(step_file_path: str) -> Optional[Dict[str, int]]:
    """
    Parse STEP file to extract assembly structure and part counts.
    
    Returns:
        {
            "DIN 1026 - U 160 - 600": 1,
            "EN 10210-2 - 88,9 x 4 - 65": 2,
            "Plaatdeel 014": 1,
            ...
        }
        or None if parsing fails
    """
```

**Waarom bestaat dit?**
- STEP-bestanden bevatten metadata over onderdelen (NEXT_ASSEMBLY_USAGE_OCCURRENCE, PRODUCT_DEFINITION)
- Deze metadata bevat vaak **semantische informatie** die niet uit geometrie af te leiden is:
  - Standaard profiel normen: "DIN 1026 - U 160 - 600"
  - Leverancier/catalog identifiers: "EN 10210-2"
  - Projectspecifieke naamgeving: "Plaatdeel 014"

---

## 2. Huidige Classificatie Pipeline

```
┌─────────────────────────────────────────────────────────────────────┐
│                    STEP File Input                                   │
└─────────────────────┬───────────────────────────────────────────────┘
                      │
        ┌─────────────┴─────────────┐
        │                           │
        ▼                           ▼
┌───────────────────┐       ┌──────────────────┐
│  STEP Parser      │       │  CadQuery/OCP    │
│  (Metadata)       │       │  (Geometrie)     │
│                   │       │                  │
│ • Partnamen       │       │ • Solids         │
│ • Assembly struct │       │ • Faces/Edges    │
│ • Counts          │       │ • Volume/BBox    │
└─────────┬─────────┘       └────────┬─────────┘
          │                          │
          │   Returns None           │
          │   voor 31686-080 ❌      │
          │                          │
          └──────────┬───────────────┘
                     │
                     ▼
         ┌───────────────────────┐
         │  analyze_assembly()   │
         │                       │
         │  1. Group solids      │
         │  2. Match names       │
         └───────────┬───────────┘
                     │
                     ▼
         ┌───────────────────────────────────────────┐
         │  Classificatie (PER PART)                 │
         │                                           │
         │  IF part_name exists:                     │
         │     IF "DIN/EN/ISO" in name:              │
         │        ✓ class = "anders" (gekocht)       │
         │        ⛔ Stop hier! Geen geometrie check  │
         │                                           │
         │  ELSE (no name OR no DIN/EN/ISO):         │
         │     🔧 classify_solid(solid)               │
         │        → Geometrie-based rules            │
         │        → Returns "plaat"/"profiel"/"anders"│
         └───────────────┬───────────────────────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │  BOMItem aanmaken    │
              │  • part_class        │
              │  • part_name         │
              │  • classification_   │
              │    trace             │
              └──────────────────────┘
```

---

## 3. Huidige Architectuur: Naam-First Strategie

### Classificatie Beslisboom (Huidige Implementatie)

```python
# In analyze_assembly() - lines 900-920

part_class = None

# STAP 1: Check naam EERST (naam-based heuristic)
if part_name:
    name_upper = part_name.upper()
    is_standard = any(std in name_upper for std in ['DIN ', 'DIN-', 'EN ', 'EN-', 'ISO ', 'ISO-'])
    
    if is_standard:
        part_class = "anders"  # ✓ Gekocht profiel, niet produceren
        # ⛔ Geometrie wordt NIET gecheckt!

# STAP 2: Geometrie-fallback (alleen als geen naam OF geen standard label)
if part_class is None:
    part_class, class_trace = classify_solid(solid, return_trace=True)
```

### Wat gebeurt er als STEP parser faalt?

Voor **31686-080.stp:**
```python
parse_step_assembly_structure('../stepfiles/31686-080.stp')
# Returns: None ❌
```

**Gevolg:**
1. `part_name` wordt `Part_1`, `Part_2`, ... (generiek)
2. Geen "DIN/EN/ISO" labels beschikbaar
3. **Alle delen gaan door geometrie-classificatie** → `classify_solid()`
4. Thick elongated standard profiles → geometrie zegt "plaat" (top2 faces > 50%)
5. ❌ Misclassificatie: 10 plaat vs expected 8 plaat

**Ontbrekende namen (31686-080 reference XML):**
- "DIN 1026 - U 160 - 600" → lost, wordt "Plaatdeel 014" → classified as plaat ❌
- "EN 10210-2 - 88,9 x 4 - 65" → lost, wordt "Plaatdeel 016" → classified as plaat ❌

---

## 4. Probleem: Fragiele Naam-Based Architectuur

### Waarom is dit niet generiek?

| Aspect | Huidige Implementatie | Probleem |
|--------|----------------------|----------|
| **STEP parser faalt** | Geen partnamen → geen DIN/EN/ISO detectie | ❌ 31686-080 failure |
| **Geen metadata** | Veel STEP-bestanden hebben generieke namen | ❌ "Part1", "Solid1" |
| **Format wijzigingen** | NEXT_ASSEMBLY_USAGE_OCCURRENCE niet altijd aanwezig | ❌ Parser returns None |
| **Namen inconsistent** | "DIN1026" vs "DIN 1026" vs "DIN-1026" | ❌ Regex mist varianten |
| **Naam in body** | Sommige STEP files hebben namen in PRODUCT_DEFINITION body | ❌ Niet geparst |

### Wat je eigenlijk hebt gebouwd:

```
┌────────────────────────────────────────┐
│  Heuristic-Based Classifier            │
│                                        │
│  Priority:                             │
│  1. Naam bevat "DIN/EN/ISO" → anders  │   ← Fragiel!
│  2. Geometrie top2 > 50% → plaat      │   ← Te breed!
│  3. Length ratio > 5 → profiel         │
│  4. Default → anders                   │
│                                        │
│  ⚠️ First-match logica = rigide        │
│  ⚠️ Geen ambiguity handling            │
│  ⚠️ Geen score/confidence              │
└────────────────────────────────────────┘
```

---

## 5. Generieke Oplossing (Architectuur Voorstel)

### 5.1 Feature Extraction (Eenmalig, Uniform)

```python
def extract_manufacturing_features(solid) -> ManufacturingFeatures:
    """
    Eén extractiestap die alle relevante features berekent.
    Geen classificatie - alleen meten.
    """
    return ManufacturingFeatures(
        # Geometry
        volume=...,
        surface_area=...,
        bbox_dims=[smallest, middle, longest],
        
        # Topology
        face_count=...,
        edge_count=...,
        planar_face_area_pct=...,
        cylindrical_face_area_pct=...,
        
        # Shape Features
        top2_face_area_pct=...,
        aspect_ratio=longest / middle,
        thickness_ratio=smallest / middle,
        cross_section_ratio=smallest / middle,
        
        # Profile Features (cross-section)
        has_constant_cross_section=...,
        cross_section_perimeter=...,
        hollow_ratio=...,
        
        # Metadata (indien beschikbaar)
        original_name=...,  # van STEP parser
        has_standard_label=("DIN" in name or "EN" in name),
    )
```

### 5.2 Twee-Fasen Classificatie

```
Phase 1: Manufacturing Family
─────────────────────────────
                ┌─────────────────────┐
                │  Feature Extractor  │
                │  (shape + topology) │
                └──────────┬──────────┘
                           │
                ┌──────────▼──────────┐
                │  Family Classifier  │
                │                     │
                │  ML/Score-based:    │
                │  • SHEET            │
                │  • PROFILE          │
                │  • OTHER            │
                └──────────┬──────────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
Phase 2: ▼                  ▼                  ▼
Subtype  SHEET              PROFILE            OTHER
         ├─ flat_plate      ├─ standard        ├─ machined
         ├─ bent_sheet      │   (DIN/EN)       ├─ complex
         └─ formed          ├─ custom          └─ assembly
                            └─ hollow
```

### 5.3 Score-Based Classificatie (vs First-Match)

**Huidige (First-Match):**
```python
if "DIN" in name:
    return "anders"  # ✓ Eerste match wint
elif top2_pct > 50:
    return "plaat"   # ❌ Wordt nooit gecheckt voor DIN parts
```

**Generiek (Score-Based):**
```python
scores = {
    "sheet": calculate_sheet_score(features),    # 3.2
    "profile": calculate_profile_score(features), # 4.8
    "other": calculate_other_score(features),     # 2.1
}

# Winnaar = hoogste score
winner = max(scores, key=scores.get)  # "profile"
margin = sorted(scores.values())[-1] - sorted(scores.values())[-2]  # 1.6

if margin < AMBIGUOUS_THRESHOLD:
    # Lage marge → ambiguous → extra checks
    if features.has_standard_label:
        return "other"  # ✓ Metadata als tiebreaker
    else:
        # Trigger unfold probe of advanced checks
        ...
```

### 5.4 Unfold Triggering (Ontkoppeld van Class)

**Huidig:** Class bepaalt unfold (bent_sheet → unfold)  
**Generiek:** Unfold probe op kandidaten vóór definitieve classificatie

```python
def classify_solid_robust(solid, features):
    # Phase 1: Initial family
    family_scores = calculate_family_scores(features)
    
    # If sheet candidate with bends suspected:
    if family_scores["sheet"] > 2.0 and features.edge_count > 12:
        # Probe: Can we unfold it?
        unfold_result = try_unfold_probe(solid)
        
        if unfold_result.success and unfold_result.bend_count > 0:
            # ✓ Bent sheet confirmed
            return "sheet", "bent_sheet", unfold_result
    
    # Continue with normal classification...
```

---

## 6. Decision Trace (Uitlegbaar)

**Huidige trace (Score Mode):**
```python
{
    "mode": "score",
    "features": {
        "top2_pct": 52.2,
        "aspect_ratio": 9.23,
        "thickness_ratio": 0.108
    },
    "scores": {
        "sheet": 3.5,
        "profile": 2.8,
        "other": 1.2
    },
    "decision": "sheet",
    "margin": 0.7,
    "is_ambiguous": true
}
```

**Generieke trace (uitgebreid):**
```python
{
    "phase1_family": "sheet",
    "phase1_scores": {"sheet": 3.5, "profile": 2.8, "other": 1.2},
    "phase1_margin": 0.7,
    "phase1_ambiguous": true,
    
    "unfold_probe": {
        "triggered": true,
        "success": false,
        "reason": "failed_to_detect_base_face"
    },
    
    "metadata_hints": {
        "has_standard_label": false,
        "original_name": "Plaatdeel 014",
        "name_suggests_profile": false
    },
    
    "tiebreaker": "geometry_confidence",
    "final_decision": "sheet",
    "confidence": 0.68,
    
    "why_not_profile": "cross_section not constant (perimeter variance 23%)",
    "why_not_other": "no complex surfaces (93% planar)"
}
```

---

## 7. Voor 31686-080: Wat Nu?

### Optie A: Geometrie-Based Standard Profile Detectie (Robust)

Bouw fallback die DIN/EN profielen herkent op **cross-section shape**:

```python
def detect_standard_profile_geometry(solid, features) -> Optional[str]:
    """
    Detecteer DIN/EN profielen op basis van cross-sectie vorm.
    Fallback voor als STEP namen ontbreken.
    """
    # U-profiel (DIN 1026): C-vorm met open bovenkant
    if is_u_section_cross_section(solid):
        return "standard_u_profile"
    
    # RHS/SHS (EN 10210-2): Holle vierkante/rechthoekige buis
    if is_hollow_rectangular(features):
        return "standard_rhs"
    
    # CHS (EN 10210-2): Holle ronde buis  
    if features.cylindrical_pct > 0.8 and features.hollow_ratio > 0.3:
        return "standard_chs"
    
    # L-profiel (DIN EN 10056): Hoekprofiel
    if is_angle_cross_section(solid):
        return "standard_angle"
    
    return None
```

### Optie B: Repair STEP Parser (Fragiel, maar snel)

Debug waarom `parse_step_assembly_structure()` faalt voor 31686-080:

```bash
# Check STEP file format
grep "NEXT_ASSEMBLY_USAGE_OCCURRENCE" ../stepfiles/31686-080.stp
grep "PRODUCT_DEFINITION" ../stepfiles/31686-080.stp | head -20
```

Mogelijk probleem:
- Geen assembly structure (single compound zonder NAUO entries)
- Namen alleen in PRODUCT_DEFINITION body (niet in NAUO)
- Format variant (AP203 vs AP214)

### Optie C: Hybrid (Naam + Geometrie Beide)

Gebruik naam als **extra feature** in score model, niet als first-match:

```python
def calculate_other_score(features):
    score = 0.0
    
    # Geometrie features
    if features.planar_pct < 0.5:
        score += 2.0
    
    # Metadata features (extra boost, geen veto)
    if features.has_standard_label:
        score += 1.5  # ✓ Hint, maar niet doorslaggevend
    
    return score
```

---

## 8. Aanbeveling

**Voor 31686-080 (korte termijn):**
→ **Optie A**: Bouw geometrie-based standard profile detector
   - Robuust (werkt zonder STEP parser)
   - Generiek (werkt op alle bestanden)
   - Uitbreidbaar (nieuwe profielen toevoegen)

**Voor architectuur (lange termijn):**
→ **Optie C**: Hybrid model met score-based beslissing
   - Naam als feature, niet als first-match regel
   - Geometrie + topology als primaire bron
   - Metadata als confidence boost

**Implementatiestappen:**
1. ✓ Centraliseer thresholds (done: `classification_variables.py`)
2. ✓ Score-based classificatie (done: `classify_solid_scored()`)
3. ✓ Decision trace (done: `classification_trace` field)
4. ⏳ Cross-section profile detection (geometrie-based standard profile)
5. ⏳ Unfold probe (before final classification)
6. ⏳ Confusion matrix validatie (per klasse)
7. ⏳ Ambiguity handling (margin < threshold → extra checks)

---

## 9. Antwoord op Je Vragen

### "Dus als ik het goed begrijp ga je dan op basis van geometrie classificeren?"

**Ja, maar:**
- Huidige implementatie: Geometrie is **fallback** (alleen als naam faalt)
- Generieke oplossing: Geometrie is **primair**, naam is **hint/feature**

### "Wat is de functie van de STEP parser?"

**Huidige rol:**
- Extractie van metadata (partnamen, assembly structure)
- Gebruikt voor naam-based heuristics ("DIN/EN/ISO" → anders)
- Gebruikt voor accurate part counts in assemblies

**Probleem:**
- Faalt vaak (returns None voor 31686-080)
- Fragiel (format afhankelijk)
- First-match logica maakt geometrie classificatie unreachable voor standard profiles

### "Waar vindt deze plaats in de Generieke oplossing?"

**Huidige plaats:** Vóór geometrie classificatie (name-first strategy)  
**Generieke plaats:** Parallel met geometrie features (metadata als extra input)

```
HUIDIG (Sequential):
Namen → [IF match] → Class
      → [ELSE] → Geometrie → Class

GENERIEK (Parallel):
Namen ─┐
       ├→ Feature Vector → Score Model → Class
Geom ─┘
```


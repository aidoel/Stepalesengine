# Classification Decision Tree

**Last updated:** 2 maart 2026  
**Version:** 2.1 (Standard Profile Detection met geometry fallback)

Dit document beschrijft de **exacte volgorde** waarin classificatie-criteria worden toegepast, inclusief threshold-waarden en rationale.

---

## 📋 Overzicht: 3-Staps Classificatie

```
Stap 1: STANDARD PROFILE CHECK (geometry-based fallback)
        → Detecteert DIN/EN/ISO onderdelen zonder metadata
        → Holle buizen, variabele dikte profielen
        → Output: "anders" (gekocht, niet produceren)

Stap 2: PLATE DETECTION (face analysis + bbox)
        → Top2 faces dominant → vlakke of gezette plaat
        → Output: "plaat"

Stap 3: PROFILE DETECTION (elongated solid)
        → Solid balken, staven
        → Output: "profiel"

Default: → "anders" (machined parts, complex geometry)
```

---

## 🌳 Volledige Beslisboom

```
classify_solid(solid)
│
├─[1] STANDARD PROFILE CHECK (nieuwe stap v2.1)
│   │
│   ├─[1a] Holle Buis Detectie (ronde/vierkante buis zonder naam)
│   │     ┌──────────────────────────────────────────────────────┐
│   │     │ IF cylindrical_pct >= 60.0%                          │
│   │     │ AND volume_ratio < 0.7                               │
│   │     │ AND aspect_ratio >= 0.5                              │
│   │     │ → ANDERS (standaard holle buis, bijv. EN 10210-2)   │
│   │     └──────────────────────────────────────────────────────┘
│   │
│   ├─[1b] Variabele Dikte Detectie (UNP, I-balk zonder naam)
│   │     ┌──────────────────────────────────────────────────────┐
│   │     │ IF top2_face_area_diff > 20%                         │
│   │     │ AND length_ratio >= 5.0                              │
│   │     │ AND NOT is_bent_sheet                                │
│   │     │ → ANDERS (standaard profiel, bijv. DIN 1026 UNP)    │
│   │     └──────────────────────────────────────────────────────┘
│   │     
│   │     Exclusie: Gezette platen (bent sheet)
│   │     ┌──────────────────────────────────────────────────────┐
│   │     │ IF has_large_radius_edges (>1mm) AND edge_count > 8 │
│   │     │ → Skip variabele dikte check (dit is gezette plaat)  │
│   │     └──────────────────────────────────────────────────────┘
│   │
│   └─ ELSE → Continue naar Stap 2
│
├─[2] PLATE DETECTION
│   │
│   ├─[2a] Face-Based Plate (primary method)
│   │     ┌──────────────────────────────────────────────────────┐
│   │     │ IF top2_face_pct >= 50.0%                           │
│   │     │ → PLAAT (top 2 faces domineren oppervlak)          │
│   │     └──────────────────────────────────────────────────────┘
│   │
│   ├─[2b] Thin Plate (bbox fallback)
│   │     ┌──────────────────────────────────────────────────────┐
│   │     │ IF smallest < 25mm                                   │
│   │     │ AND thickness_ratio < 0.15                           │
│   │     │ AND aspect_ratio > 5.0                               │
│   │     │ → PLAAT (dunne langwerpige plaat)                   │
│   │     └──────────────────────────────────────────────────────┘
│   │
│   └─ ELSE → Continue naar Stap 3
│
├─[3] PROFILE DETECTION
│   │
│   ├─[3a] Profile Basis Check
│   │     ┌──────────────────────────────────────────────────────┐
│   │     │ IF smallest >= 5mm                                   │
│   │     │ AND length_ratio >= 5.0                              │
│   │     │ AND cross_ratio 0.5-2.0                              │
│   │     │ → Continue naar volume check                         │
│   │     └──────────────────────────────────────────────────────┘
│   │
│   ├─[3b] Solid Profile (strong)
│   │     ┌──────────────────────────────────────────────────────┐
│   │     │ IF volume_ratio > 0.5                                │
│   │     │ → PROFIEL (solid balk/staf)                         │
│   │     └──────────────────────────────────────────────────────┘
│   │
│   ├─[3c] Profile Ambiguous (weak, gebruik SA/V tiebreaker)
│   │     ┌──────────────────────────────────────────────────────┐
│   │     │ IF volume_ratio 0.15-0.5                             │
│   │     │ AND sa_v_ratio < 1.2                                 │
│   │     │ → PROFIEL (lagere SA/V = solid)                     │
│   │     └──────────────────────────────────────────────────────┘
│   │
│   └─ ELSE → Continue naar Default
│
└─[DEFAULT] → ANDERS (alles wat niet past: machined, complex, etc.)
```

---

## 📊 Criteria Tabel (Alfabetisch)

| Criterium | Threshold | Gebruikt In Stap | Omschrijving |
|-----------|-----------|------------------|--------------|
| **aspect_ratio** | > 5.0 | [2b] Thin Plate<br>[1b] Variable Thickness | longest / middle |
| **cross_ratio** | 0.5 - 2.0 | [3a] Profile Basis | smallest / middle (rechthoekige doorsnede) |
| **cylindrical_pct** | ≥ 60.0% | [1a] Hollow Tube | % cylindrische vlakken |
| **edge_count** | > 8 | [1b] Bent Sheet Exclusion | Aantal edges (veel = gezet) |
| **large_radius_edges** | ≥ 1.0 mm | [1b] Bent Sheet Exclusion | Heeft buigradius ≥1mm (bent sheet) |
| **length_ratio** | ≥ 5.0 | [3a] Profile Basis<br>[1b] Variable Thickness | longest / smallest (elongated) |
| **sa_v_ratio** | < 1.2 | [3c] Profile Ambiguous | surface_area / volume (cm⁻¹) |
| **smallest** | < 25.0 mm | [2b] Thin Plate | Kleinste bbox dimensie |
| | ≥ 5.0 mm | [3a] Profile Basis | Geen dunne sheet |
| **thickness_ratio** | < 0.15 | [2b] Thin Plate | smallest / middle |
| **top2_face_area_diff** | > 20% | [1b] Variable Thickness | Top 2 faces verschillen >20% (variabel) |
| **top2_face_pct** | ≥ 50.0% | [2a] Face-Based Plate | % oppervlak in top 2 vlakken |
| **volume_ratio** | < 0.7 | [1a] Hollow Tube | volume / bbox volume (hol) |
| | ≥ 0.15 - 0.5 | [3c] Profile Ambiguous | Medium fill |
| | > 0.5 | [3b] Solid Profile | High fill (solid) |

---

## 🔍 Waarom Deze Volgorde?

### Stap 1 EERST: Standard Profile Detection

**Rationale:**
- Standaard onderdelen (DIN/EN/ISO) zijn **gekocht, niet geproduceerd**
- Moeten "anders" worden, **niet** plaat/profiel
- Zonder STEP namen zou bijv. korte holle buis als "plaat" eindigen (top2 faces 94%!)

**Voorbeeld problemen opgelost:**
- EN 10210-2 ronde buis (Ø89×65mm): cylindrisch 94% + hol → **anders** ✓
- DIN 1026 UNP160 (variabele dikte): top2 faces 52% maar variabel → **anders** ✓

### Stap 2 DAARNA: Plate Detection

**Rationale:**
- Als het GEEN standaard profiel is, check dan of het plaatwerk is
- Top2 faces > 50% = sterk signaal voor plaat
- Dunne platen vangen we via bbox fallback

### Stap 3 LAATSTE: Profile Detection

**Rationale:**
- Solid elongated vormen die geen plaat zijn
- Volume_ratio belangrijk: hol vs solid
- SA/V ratio als tiebreaker bij ambiguous cases

---

## 🎯 v2.1 Updates (2 maart 2026)

### Toegevoegd:

1. **Holle Buis Detectie** (geometry-based)
   - Cylindrical % + volume_ratio + aspect_ratio
   - Werkt zonder STEP namen
   - Voorkomt korte buizen als "plaat"

2. **Variabele Dikte Detectie** (geometry-based)
   - Top2 face area verschil > 20%
   - Elongated (L/D ≥ 5)
   - Exclusie voor gezette platen (bent sheet check)
   - Voorkomt UNP/I-balk als "plaat"

3. **Gezette Plaat Exclusie**
   - Large radius edges (≥1mm) = bends
   - Edge count > 8 = veel geometrie
   - Voorkomt false positives op dikte-variatie check

### Configuratie:

Alle nieuwe thresholds in `classification_variables.py`:

```python
# Hollow tube detection
STANDARD_TUBE_CYLINDRICAL_MIN_PCT = 60.0
STANDARD_TUBE_VOLUME_RATIO_MAX = 0.7
STANDARD_TUBE_ASPECT_MIN = 0.5

# Variable thickness detection
STANDARD_PROFILE_ELONGATED_LENGTH_RATIO_MIN = 5.0
STANDARD_PROFILE_FACE_AREA_TOLERANCE = 0.20

# Bent sheet exclusion
BENT_SHEET_LARGE_RADIUS_MIN_MM = 1.0
BENT_SHEET_MIN_EDGE_COUNT = 8
```

---

## 🧪 Test Cases

### Test 1: EN 10210-2 (Korte Ronde Buis)

**Input:** Ø88.9×65mm (wanddikte 4mm)

**Geometrie:**
- cylindrical_pct: **94.2%** ✓
- volume_ratio: **0.135** ✓ (<0.7)
- aspect_ratio: **1.0** ✓ (≥0.5)

**Huidig (v2.0):** plaat (top2 faces 94%)  
**Nieuw (v2.1):** **anders** ✓ (holle buis detectie)

---

### Test 2: DIN 1026 UNP160 (U-Profiel)

**Input:** 65×160×600mm (variabele dikte 7-13mm)

**Geometrie:**
- top2_face_pct: **52.2%** (net boven grens)
- top2_face_area_diff: **>20%** ✓ (variabel!)
- length_ratio: **9.2** ✓ (≥5.0)
- is_bent_sheet: False (geen grote radius edges)

**Huidig (v2.0):** plaat (top2 faces 52%)  
**Nieuw (v2.1):** **anders** ✓ (variabele dikte detectie)

---

### Test 3: Gezette Plaat (3 bends, 4mm dikte)

**Input:** Plaat 300×150×50mm, 3 bends r=3mm

**Geometrie:**
- top2_face_area_diff: **20%** (binnen/buiten radius)
- length_ratio: **7.5** (≥5.0)
- has_large_radius_edges: **True** (r=3mm ≥1mm) ✓
- edge_count: **18** (>8) ✓

**Check:** Bent sheet exclusion triggered  
**Output:** **plaat** ✓ (skip variabele dikte check)

---

## 🔬 Debug & Validatie

### Audit Tool:

```bash
python analyze_two_parts.py ../stepfiles/31686-080.stp
```

Toont per part:
- Alle geometrie ratio's
- Welke regels triggeren
- Waarom huidige class gekozen
- Voorgestelde nieuwe class

### Classification Trace:

BOMItem heeft nu `classification_trace` field:

```python
{
    "standard_profile_check": {
        "hollow_tube": {"triggered": True, "cylindrical_pct": 94.2, ...},
        "variable_thickness": {"triggered": False, ...}
    },
    "plate_check": {
        "face_based": {"triggered": True, "top2_pct": 94.2}
    },
    "final_decision": "anders",
    "reason": "hollow_tube_detected"
}
```

### Validatie Script:

```bash
python validate_classification_only.py
```

Test op 5 referentie STEP files, toont confusion matrix per klasse.

---

## 📚 Gerelateerde Documentatie

- **[CLASSIFICATION_SCHEMA.md](CLASSIFICATION_SCHEMA.md)** — 4 classificatie categorieën (plaat/profiel/anders)
- **[CLASSIFICATION_ARCHITECTURE.md](CLASSIFICATION_ARCHITECTURE.md)** — Technische architectuur, naam vs geometrie
- **[classification_variables.py](../manufacturing_pipeline/analysis/classification_variables.py)** — Alle thresholds (⭐ single source of truth)
- **[assembly_analysis.py](../manufacturing_pipeline/analysis/assembly_analysis.py)** — Implementatie van `classify_solid()`

---

## 🎓 Lessons Learned

### 31686-080 Failure (2 maart 2026)

**Symptoom:** 10 plaat vs expected 8 plaat (2 false positives)

**Root Cause:**
1. STEP parser faalt → geen DIN/EN namen
2. Geometry-only rules miss standard profiles
3. Top2 faces trigger plaat classification → **fout**

**Fix:** v2.1 geometry-based standard profile detection

**Generic Solution:**
- ✓ Werkt zonder metadata (robuust)
- ✓ Holle buis + variabele dikte checks
- ✓ Bent sheet exclusion (voorkomt false positives)
- ✓ Alle thresholds tunable in classification_variables.py

---

**Version History:**
- v1.0: Original first-match rules (plaat → profiel → anders)
- v2.0: Face-based plate detection + score model (optioneel)
- **v2.1: Standard profile geometry fallback (deze versie)** ⭐

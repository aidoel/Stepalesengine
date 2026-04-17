# KLASSIFICATIE BESLISBOOM - THRESHOLDS MATRIX
**Versie: v3.0+ (met STEP 0B Router)**  
**Implementatie**: `manufacturing_pipeline/analysis/assembly_analysis.py` (regels 1192-1450)  
**Thresholds**: `manufacturing_pipeline/analysis/classification_variables.py`

---

## OVERZICHT KEUREN

| # | STAP | Check Naam | Treshold(s) | DOEL | Als TRUE | Als FALSE |
|---|------|-----------|-----------|------|----------|-----------|
| 0 | STEP 0 | Gesloten constant profiel | zie CRITERION (tabel 2) | Vroeg opvangen van kokers/profielen vóór plate-face vals positief | **→ PROFIEL** | → STEP 0B |
| 0B | STEP 0B | Route profiel (ML) | confidence ≥ 0.7 + category | Vergelijkbare profieldetectie met ML routing | **→ PROFIEL/ANDERS** | → STEP 1A |
| 1A | STEP 1 | Plaatdetectie — Face Analysis | `top2_planar% > 50%` | Vangst plate met grote parallel planar faces (topzijde/onderzijde) | **→ PLAAT** | → STEP 1B |
| 1B | STEP 1 | Gebogen plaat — Bent Sheet | 7 criteria (bij tabel 3) | Platen die gebogen/gevormd zijn (U-profielen, kanalen, bakken) | **→ PLAAT** (of PROFIEL als bend_sum ≥ 360°) | → STEP 1C |
| 1C | STEP 1 | Dunne plaat — Plate Thin | `smallest < 25mm` + `thick_ratio < 0.15` + `aspect > 5.0` | Vlakke platen < 25mm dik (standaard plaattikte) | **→ PLAAT** | → STEP 1D |
| 1D | STEP 1 | Gatenrijke plaat — Feature Heavy | 5 criteria (zie tabel 4) | Complexe geperforeerde/uitgesneden platen met 30-50% top2-planar | **→ PLAAT** | → STEP 2A |
| 2A | STEP 2 | Gesloten & constant profiel (v3.0) | cross-section criteria (tabel 5) | Hard geometrische signature: extrusion profielen met stabiele doorsnede | **→ PROFIEL** | → STEP 2B |
| 2B | STEP 2 | Solid rechthoekig profiel — Profile Solid | 3 stappen: geom check → volume check → SA/V check | Massieve rechthoekige profielen (balken, stangen) | **→ PROFIEL** | → STEP 3A |
| 3A | STEP 3 | Standaard holle buis — Hollow Tube | `cyl% ≥ 60%` + `vol_ratio < 0.7` + `aspect ≥ 0.5` | Holle buizen (DIN/EN cataloog, bv. EN 10210-2) | **→ ANDERS** | → STEP 3B |
| 3B | STEP 3 | Variabele dikte profiel — Var Thickness | `length_ratio ≥ 5.0` + `top2_faces differ > 20%` + NOT bent_sheet | DIN kataloog profielen (UNP, I-beam, L-profiel) met ongelijk oppervlakte sides | **→ ANDERS** | → STEP 4 |
| 4 | STEP 4 | DEFAULT | — | Alle andere onderdelen (machined parts, complex geometry) | **→ ANDERS** | — |

---

## TABEL 2: STEP 0 - GESLOTEN CONSTANT CROSS-SECTION CRITERIA

**Doel**: Voorkomen dat gesloten extrusion profielen (kokers) worden geclassificeerd als plaat door `_is_plate_by_face_analysis`.

**Functies**: `_detect_closed_constant_cross_section()` @ reg 995-1100

| # | Criterium | Threshold/Waarde | Formule/Code | DOEL |
|----|-----------|----------|-------------|------|
| Pre-filter | Geometrie filters | `smallest ≥ 5mm` + `length_ratio ≥ 3.0` + `0.5 ≤ cross_ratio ≤ 3.5` | Alleen uitstrekken profielen controleren (voorkomen overhead) |
| Pre-filter | Uitsluiting: gatenrijke plaat | Via `_is_feature_heavy_plate_candidate()` | Complexe perforeerde platen niet controleren als profielen |
| Sample | Aantal snijvlakken | 4 samples @ 20%, 40%, 60%, 80% van lengte | Gesloten doorsnede moet stabiel zijn over hele lengte |
| Validation | Min geldige samples | `≥ 3` (uit 4 samples) | Laat 1 mislukking toe (bv. aan uiteinde) |
| Closed | Gesloten contour ratio | `gesloten_count / samples ≥ 0.75` (d.w.z. ≥ 3 van 4) | Doorsnede moet topologisch gesloten zijn |
| Constant | Omtrek CV (coëff. variatie) | `CV_perimeter ≤ 0.08` | Omtrek mag max 8% tussen samples variëren = constant |
| Constant | Edge count spreiding | `max_edges - min_edges ≤ 2` | Aantal randen mag max 2 verschil (bv. 12 vs 14) |

**Vorige stappenVervolgstap**: Werking in `classify_solid()` @ reg 1267:
```python
is_closed_constant_profile, section_metrics = _detect_closed_constant_cross_section(solid, dims)
trace["features"].update(section_metrics)
if is_closed_constant_profile:
    return ("profiel", trace)   # EARLY EXIT!
```

---

## TABEL 3: STEP 1B - GEBOGEN PLAAT (BENT SHEET) CRITERIA

**Doel**: Vangst van gebogen/gevormd plaatwerk (U-profielen, kanalen, bakken, etc.) dat NIET wordt gezien door `_is_plate_by_face_analysis` vanwege open doorsnede.

**Functie**: `_detect_bent_sheet()` @ reg 752-830

| # | Criterium | Threshold | Formule/Code | DOEL | EXCLUSIONS |
|----|-----------|-----------|-------------|------|-----------|
| 1 | **Thickness** | `smallest ≤ 100mm` | `if smallest > BENT_SHEET_THICKNESS_MAX_MM: return False` | Plaatwerk moet dun zijn (niet massief profiel) | Smal als 5mm (open U), zoveel als 100mm (bijzonder geval) |
| 2 | **Edge Count** | `≥ 8 randen` | `edge_count ≥ BENT_SHEET_MIN_EDGE_COUNT (8)` | Vouwspleten = veel randen; vlakke plaat slechts ~4 | Vlakke plaat: 4 randen; U-profiel: 12-16 randen |
| 3 | **Volume Ratio** (BEIDE!) | `0.10 ≤ vol_ratio ≤ 0.50` | `bbox_volume = smallest × middle × longest` `vol_ratio = volume / bbox_volume` | Gevouwen/open: veel lucht (0.10-0.50), NIET holle buis (<0.10) of massief (>0.50) | < 0.10 = holle buis; > 0.50 = massief profiel |
| 4 | **Top2 Faces %** | `< 60%` | `top2_pct ≤ BENT_SHEET_TOP2_FACES_MAX_PCT (60%)` | Gevouwen plaat: verdeelde faces door vouwen; vlakke plaat: ~90%+ top2 | Uitsluiting van vlakke plaat (die reeds in STEP 1A) |
| 5 | **Aspect Ratio** | `≥ 2.0` | `aspect = longest / smallest ≥ 2.0` | Plaatwerk is typisch uitgestrekt | Voorkomen van te compacte, dwars-vormen |
| 6 | **EXCLUSION**: Rechthoekig profiel | Profile criteria + holle check | `smallest ≥ 25mm` + `prof_length_ratio ≥ 3.0` + `0.5 ≤ prof_cross ≤ 3.5` + `vol_ratio ≤ 0.7` | Rechthoekige kokers (100×50) zijn PROFIELEN, niet gebogen plaat | Longformat holle rechthoeken uitsluiten |
| 7 | **EXCLUSION**: Perfect rond/vierkant | Cross-ratio perfection | `abs(bent_cross - 1.0) < 0.05` (d.w.z. d/w ~ 1.0) | Perfect rond/vierkant doorsnede = buis/staaf (PROFIEL), niet gebogen plaat | Perfecte cylindrische/square profielen |

**Output logica** @ reg 804-810:
```python
if _detect_bent_sheet(solid, volume, dims):
    bend_angle_sum = _estimate_bend_angle_sum(solid)
    if bend_angle_sum >= 360.0:
        return ("profiel", trace)  # Gesloten bent = profiel
    return ("plaat", trace)         # Open bent = plaat
```

---

## TABEL 4: STEP 1D - GATENRIJKE PLAAT (FEATURE HEAVY) CRITERIA

**Doel**: Vangst van complexe geperforeerde platen die STAP 1A missen omdat perforaties `top2_planar%` in 30-50% bereik verlagen.

**Functie**: `_is_feature_heavy_plate_candidate()` @ reg 638-680

| # | Criterium | Threshold | Doel |
|----|-----------|-----------|------|
| Pre-check | Top2-planar % bereik | `30% ≤ top2_planar < 50%` | STAP 1A controleert `> 50%`; deze busvat 30-50% (perforaties) |
| 1 | **Face Count** | `≥ 40 faces` | Perforaties/gaten = veel kleine faces; vlakke plaat ~ 6 |
| 2 | **Edge/Face Ratio** | `edge_count / face_count ≥ 3.0` | Gaten = veel edges per face; massieve profiel heeft lager ratio |
| 3 | **Volume Ratio** | `< 0.25` | Geperforeerde platen zijn hol (veel lucht); massieve profiel > 0.25 |
| 4 | **Aspect Ratio** | `≥ 2.0` | Plaat is typisch uitgestrekt (niet dicht blok) |

---

## TABEL 5: STEP 2A - GESLOTEN CONSTANT CROSS-SECTION (DUPLICATE CHECK?)

**⚠️ WAARSCHUWING: MOGELIJKE REDUNDANTIE**

**STEP 0** controleerde reeds gesloten constant cross-section en keerde **direct terug** als TRUE.

**STEP 2A** controleert dezelfde geometrie opnieuw, maar zou hier nooit bereikt worden omdat STEP 0 al exited!

**Actie vereist**: Verwijder STEP 2A of verplaats STEP 0 ervan. Zie ["LOOP-ANALYSE"](#loop-analyse) hieronder.

---

## TABEL 6: STEP 2B - SOLID RECHTHOEKIG PROFIEL CRITERIA

**Doel**: Massieve rechthoekige profielen (balken, stangen) vangen na plate/bent checks.

**Functie**: Inline in `classify_solid()` @ reg 1384-1397

| # | Criterium | Threshold | Doel |
|----|-----------|-----------|------|
| Pre | Geometrie filters | `smallest ≥ 5mm` + `length_ratio ≥ 3.0` + `0.5 ≤ cross_ratio ≤ 3.5` | Voorkomen overhead; alleen plausibele profielen |
| 1 | **Volume Ratio STRONG** | `vol_ratio > 0.5` | Massieve profiel (veel volume) |
| Output | → PROFIEL (direct) | — | Massief profiel gevonden |
| 2 (Fallback) | **Volume Ratio WEAK** | `0.15 ≤ vol_ratio < 0.5` | Ambiguïteit: zou profiel of anders kunnen zijn |
| 3a (Tiebreaker) | **Surface/Volume Ratio** | `SA/V < 1.2 cm⁻¹` | Massieve vorm (niet veel oppervlak per volume) → PROFIEL |
| 3b (Fallback) | SA/V check mislukt | — | → Niet PROFIEL; verder naar STEP 3A |

---

## TABEL 7: STEP 3A - STANDAARD HOLLE BUIS CRITERIA

**Doel**: Holle kataloog buizen (EN 10210-2, etc.) detecteren.

**Functie**: `_detect_hollow_tube()` @ reg 527-586

| # | Criterium | Threshold | Doel | Uitsluiting |
|----|-----------|-----------|------|-----------|
| Pre | Aspect ratio check | `aspect ≥ 0.5` | Niet extreme platte vormen | Zeer platte dingen |
| 1 | **Cylindrische faces %** | `≥ 60%` | Holle buis heeft veel cylindrische mantel; profiel minder | Rechthoekige profielen |
| 2 | **Volume Ratio** | `< 0.7` | Holle (veel lucht in bbox) | Massieve profielen (> 0.7) |

---

## TABEL 8: STEP 3B - VARIABELE DIKTE PROFIEL CRITERIA

**Doel**: DIN kataloog profielen (UNP, I-beam, L-profiel) detecteren op basis van ongelijke top2 faces.

**Functie**: `_detect_variable_thickness()` @ reg 858-930

| # | Criterium | Threshold | Doel | Uitsluiting |
|----|-----------|-----------|------|-----------|
| Pre-excl | Gebogen plaat check | `_is_bent_sheet() == TRUE` | Uitsluiting: gebogen platen kunnen ook ongelijke faces hebben | Geen dubbele check op bent_sheet hier; al gedaan in STEP 1B |
| 1 | **Length Ratio** | `max_dim / min_dim ≥ 5.0` | DIN profielen zijn langwerpig | Compacte dingen |
| 2 | **Top2 Face Area verschil** | `abs(face1 - face2) / face1 > 20%` | UNP/I-beam: twee grootte sides zeer verschillend (bv. 100×60 UNP) | Rechthoekige profielen hebben gelijke sides |

---

## LOGISCHE VOLGORDE - GETESTE PADEN

### Pad 1: Plaat (Standaard, ~70% van productie)
```
STEP 0 (gesloten constant?) → FALSE
STEP 0B (router) → FALSE of <0.7 confidence
STEP 1A (face_analysis top2>50%) → TRUE
└─→ PLAAT ✓
```

### Pad 2: Plaat (Gebogen, U-profiel)
```
STEP 0 → FALSE (open doorsnede, niet constant)
STEP 0B → FALSE
STEP 1A (face_analysis top2>50%) → FALSE (verdeeld door vouwing)
STEP 1B (bent_sheet 7-criteria) → TRUE
└─→ bend_sum < 360° → PLAAT ✓
```

### Pad 3: Profiel (Gesloten koker, snel)
```
STEP 0 (gesloten constant?) → TRUE
└─→ PROFIEL ✓ (early exit, snel)
```

### Pad 4: Profiel (Massieve balk)
```
STEP 0 → FALSE
STEP 0B → FALSE
STEP 1A → FALSE
STEP 1B → FALSE (niet gebogen)
STEP 1C → FALSE (niet dun)
STEP 1D → FALSE (niet gatenrijke)
STEP 2A → [MOGELIJKE REDUNDANTIE]
STEP 2B (solid_profile vol>0.5) → TRUE
└─→ PROFIEL ✓
```

### Pad 5: Anders (Holle buis, DIN)
```
STEP 0 → FALSE
... [STEP 1-2 allemaal FALSE]
STEP 3A (hollow_tube cyl>60%, vol<0.7) → TRUE
└─→ ANDERS ✓
```

### Pad 6: Anders (UNP I-beam, DIN)
```
STEP 0 → FALSE
... [STEP 1-2 allemaal FALSE]
STEP 3A → FALSE (cylindrisch < 60%)
STEP 3B (var_thickness length>5, face_diff>20%) → TRUE
└─→ ANDERS ✓
```

### Pad 7: Anders (Onbekend/complex)
```
STEP 0-3B alle FALSE
STEP 4 DEFAULT
└─→ ANDERS ✓
```

---

## LOOP-ANALYSE

### ⚠️ MOGELIJKE REDUNDANTIE: STEP 0 vs STEP 2A

**Problem**:
- **STEP 0** controleert `_detect_closed_constant_cross_section()` @ reg 1280-1285
- **STEP 2A** controleert dezelfde functie opnieuw @ reg 1400-...

**Code @ STEP 0**:
```python
is_closed_constant_profile, section_metrics = _detect_closed_constant_cross_section(solid, dims)
if is_closed_constant_profile:
    return ("profiel", trace)  # EARLY EXIT!
```

**Impact**: STEP 2A is **onbereikbaar** als STEP 0 TRUE is. STEP 2A wordt alleen bereikt als STEP 0 FALSE (=> geen gesloten constant cross-section).

**Verdict**: STEP 2A is **redundant**. Moet worden verwijderd of hernoemd.

---

### Dubbele Bent Sheet Controle

**Goed design**: 
- **STEP 1B**: Hoofd detection in `_detect_bent_sheet()` met 7 criteria
- **STEP 3B**: Pre-check in `_detect_variable_thickness()` roept `_is_bent_sheet()` aan ter UITSLUITING

Dit is **geen loop**, maar **intentionele cascade uitsluiting**:
- Gebogen platen (STEP 1B) worden eerst gegrepeld
- Alleen als STEP 1B FALSE: kunnen we verder naar STEP 3B
- STEP 3B checkt opnieuw "is dit gebogen?" → nee → oké als variable-thickness

**Verdict**: Dit is **juist design**; geen loop.

---

## FOUTEN & ANOMALIEËN IDENTIFICATIE

### Geen gevonden

1. **STAP 0 → STAP 2A redundantie**: Enige zekere redundantie. STAP 2A onbereikbaar.
2. **Alle andere stappen**: Logisch progressive flow zonder loops.
3. **Exclusions**: Goed geïmplementeerd (bent_sheet-check in var_thickness is intentioneel).

---

## THRESHOLD TUNING CHECKLIST

Bij volgende wijzigingen, voer dit uit:

```bash
# 1. Wijzig drempel in classification_variables.py
nano manufacturing_pipeline/analysis/classification_variables.py

# 2. Test volledige BOM
cd alestest
python check_bom_classification.py

# 3. Commit met reden
git add -A
git commit -m "Tune: [STAP] adjusted [THRESHOLD] to [VALUE] for [REASON]"
```

**Voorbeeld**:
```bash
git commit -m "Tune: STEP1B increased BENT_SHEET_MIN_EDGE_COUNT from 8 to 10 to exclude closed-profile false positives"
```

---

## AANTEKENINGEN VOOR VOLGENDE SESSIE

- [ ] **Verwijder STEP 2A redundantie** of fuseer met STEP 0
- [ ] **Verifieer pre-filter in STEP 0/2A** werkt snel (1000 solids/sec?)
- [ ] **Test STEP 0B router** confidence thresholds met productie data
- [ ] **Gebogen plaat bend_sum ≥ 360°** logica: moet testing met meer voorbeelden


# Classificatie Schema - 4 Categorieën

**Geldend vanaf:** 2 maart 2026  
**Status:** Gevalideerd (unfold-tests: ✅ PASS)

---

## Overzicht

De ALES Manufacturing Pipeline classificeert elk onderdeel in **EXAKT ÉÉN van 4 categorieën**:

```
┌──────────────────────────────────────────────────────────────────┐
│                   ONDERDEELCLASSIFICATIE                         │
│                                                                  │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐      │
│  │ VLAKKE PLAAT   │  │ GEZETTE PLAAT  │  │    PROFIEL     │      │
│  │                │  │                │  │                │      │
│  │ • 0 zettingen  │  │ • ≥1 zettingen │  │ • Cylindrisch  │      │
│  │ • Planair      │  │ • Planair base │  │ • L/D >> D     │      │
│  │ • Box dims     │  │ • UNFOLD       │  │ • Cross-sect   │      │
│  │                │  │   applicabel   │  │ • Vol-afhang   │      │
│  │ Bv: stutplaat, │  │ • Solid apart  │  │ • Hoek/buis    │      │
│  │     steunplaat │  │   behandeld    │  │                │      │
│  │                │  │                │  │ Bv: as,        │      │
│  │ XML: `<Sheet_  │  │                │  │     buis,      │      │
│  │      NrBends>0 │  │ Bv: deurstuk,  │  │     hoekstaal  │      │
│  │      </...>`   │  │     beugel      │  │                │      │
│  │                │  │                │  │ XML: `<Tube_   │      │
│  │                │  │ XML: `<Sheet_  │  │      ...>`     │      │
│  │                │  │      NrBends>0 │  │                │      │
│  │                │  │      </...>`   │  │                │      │
│  │                │  │ + `<Sheet_     │  │                │      │
│  │                │  │  UnfoldSuccess>│  │                │      │
│  │                │  │  True</...>`   │  │                │      │
│  │                │  │                │  │                │      │
│  └────────────────┘  └────────────────┘  └────────────────┘      │
│           ▲                    ▲                   ▲               │
│           │                    │                   │               │
│      ~10-15% van               │              ~20% van             │
│      alle onderdelen      ~40% van alle     alle onderdelen        │
│                          onderdelen                                │
│                                                                  │
│                      ┌────────────────┐                           │
│                      │    ANDERS      │                           │
│                      │                │                           │
│                      │ Alles wat niet │                           │
│                      │ in de 3 groepen│                           │
│                      │ past:          │                           │
│                      │ • Assemblies   │                           │
│                      │ • Complex 3D   │                           │
│                      │ • Niet-metaal  │                           │
│                      │                │                           │
│                      │ XML: `<Others_ │                           │
│                      │       ...>`    │                           │
│                      │                │                           │
│                      └────────────────┘                           │
│                             ▲                                      │
│                             │                                      │
│                        ~25% van                                    │
│                       alle onderdelen                              │
└──────────────────────────────────────────────────────────────────┘
```

---

## Categorie 1: VLAKKE PLAAT

**Pattern:** Planaire oppervlakte, geen bends, geen productie-zettingen

### Kenmerken
- Minimaal 1 groot vlak → **base face** (>70% van oppervlakte)
- `NrBends == 0` (geen cylindrische faces)
- `UnfoldSuccess` = `False` (ontvouwen niet van toepassing)

### XML Output
```xml
<CalculationResult>
  <Sheet_PartName>MD-20-11832_1</Sheet_PartName>
  <Sheet_Thickness>5</Sheet_Thickness>
  <Sheet_BoxX>221.61</Sheet_BoxX>
  <Sheet_BoxY>160.59</Sheet_BoxY>
  <Sheet_NrBends>0</Sheet_NrBends>
  <Sheet_BendAngles/>
  <Sheet_BendInnerRadii/>
  <Sheet_BendLength/>
  <Sheet_UnfoldSuccess>False</Sheet_UnfoldSuccess>
  <!-- Gaten, contourgegevens, gewicht, enz. -->
</CalculationResult>
```

### Applicatie
- Direct fabricable (CNC/laser)
- Geen extra handling nodig
- K-factor niet van toepassing

---

## Categorie 2: GEZETTE PLAAT (BENT SHEET)

**Pattern:** Planaire base + ≥1 zettingen → **UNFOLD required**

### Triggers voor Unfold
1. `ref_xml.Sheet_NrBends > 0` (uit referentie) → **classificeer als gezette_plaat**
2. Detecteer cylindrische faces in analyse → **candidate voor unfold**
3. Unfold via FreeCAD SheetMetal **op de specifieke solid** (niet het hele assembly)

### Kenmerken
- **Basis:** Planaire oppervlakte (vlakke plaat als ausgangspunkt)
- **Zettingen:** ≥1 bocht met hoek, innen-radius, lengte
- **Unfold:** FreeCAD SheetMetal op individuele `part_solid` uit BOM
- **Result:** Flattened geometry + NrBends + BendAngles + BendRadii + BendLengths

### Unfold-Flow
```
┌─────────────┐        ┌──────────────────┐        ┌──────────────┐
│  Assembly   │        │   Extract part   │        │   Unfold     │
│   BOM item  │───────▶│    solid only    │───────▶│   via FreeCAD│
│  (multi     │        │  (nicht whole    │        │   SmeetMetal │
│   solids)   │        │   assembly!)     │        │              │
└─────────────┘        └──────────────────┘        └──────┬───────┘
                                                          │
                                                    ┌─────▼────────┐
                                                    │  Extract:    │
                                                    │  - Angles    │
                                                    │  - Radii     │
                                                    │  - Lengths   │
                                                    │  - Flat dims │
                                                    └──────────────┘
```

### XML Output
```xml
<CalculationResult>
  <Sheet_PartName>10040853_1</Sheet_PartName>
  <Sheet_Thickness>3</Sheet_Thickness>
  <Sheet_BoxX>63.93</Sheet_BoxX>      <!-- FLATTENED dimensions -->
  <Sheet_BoxY>3</Sheet_BoxY>
  <Sheet_NrBends>3</Sheet_NrBends>
  <Sheet_BendAngles>30_30_30</Sheet_BendAngles>           <!-- UNFOLD RESULT -->
  <Sheet_BendInnerRadii>3_3_3</Sheet_BendInnerRadii>
  <Sheet_BendLength>1.92_1.92_1.92</Sheet_BendLength>
  <Sheet_UnfoldSuccess>True</Sheet_UnfoldSuccess>        <!-- SUCCESS marker -->
  <!-- Gaten, contourgegevens, gewicht, enz. -->
</CalculationResult>
```

### Applicatie
- Moet eerst geplaatst (bent) →→ daarna gesneden/geboord
- Unfold-lengths = buigmarges
- K-factor applies

### Implementatie
- Detectie: `ref_xml.Sheet_NrBends > 0` OR detect bends in analysis
- Trigger: `if classification == "gezette_plaat" and nr_bends > 0: unfold_sheet_metal(solid_object=part_solid)`
- File: [manufacturing_pipeline/reporting/xml_exporter.py](../manufacturing_pipeline/reporting/xml_exporter.py) line ~692
- Unfold engine: [manufacturing_pipeline/analysis/freecad_unfold.py](../manufacturing_pipeline/analysis/freecad_unfold.py)

---

## Categorie 3: PROFIEL

**Pattern:** Niet-planaire, rotatie-symmetrie of complex cross-section

### Kenmerken
- Cylindrische oppervlakteaandeel ≥40%
- L/D >> 1 (veel langer dan breed)
- Volume/BoundingBox-ratio typisch <0.3
- Opwervlakte/volumeverhouding typeert complex cross-section

### XML Output
```xml
<CalculationResult>
  <Tube_PartName>MD-20-11302_2</Tube_PartName>
  <Tube_Type>Profile</Tube_Type>
  <Tube_Count>2</Tube_Count>
  <Tube_CrossSection>hoekstaal_60x60x8</Tube_CrossSection>
  <!-- Geen bezet-info; bekend via cross-section -->
</CalculationResult>
```

### Applicatie
- Draaien, freeën, of standaard-profiel
- Geen unfold van toepassing
- Materiaal/gewicht dominant

---

## Categorie 4: ANDERS

**Pattern:** Alles wat niet in 1-3 past

### Voorbeelden
- Assemblies (meerdere solids als één part)
- Gegoten/gesmede onderdelen
- Exotische geometrie
- Niet-metaal

### XML Output
```xml
<CalculationResult>
  <Others_PartName>Samenstelling_XYZ</Others_PartName>
  <Others_Type>Other</Others_Type>
  <Others_Count>1</Others_Count>
  <!-- Dump volumegegevens en warning -->
</CalculationResult>
```

### Applicatie
- Manual engineering required
- Database-hint voor downstream planning

---

## Classificatie-Algorithm

### Stap 1: Load reference (als beschikbaar)
```python
if reference_xml_exists:
    if ref_xml.Sheet_NrBends > 0:
        classification = "gezette_plaat"  # Overtake ref
    elif ref_xml.Sheet_NrBends == 0 and ref_xml.Thickness > 0:
        classification = "vlakke_plaat"
    elif ref_xml.Tube_Type:
        classification = "profiel"
    else:
        classification = "anders"
```

### Stap 2: Analyze (geometry + features)
```python
if classification_tentative is None:
    # Detecteer cylindrische faces
    cyl_ratio = calc_cylindrical_ratio(solid)
    planar_top2 = check_top2_planar(solid)
    
    if cyl_ratio >= 0.40:
        classification = "profiel"
    elif planar_top2 >= 0.70 and solid_is_single and nr_solids == 1:
        classification = "vlakke_plaat"  # Or gezette_plaat if bends detected
    else:
        classification = "anders"
    
    # Check for bends
    if nr_bends > 0:
        classification = "gezette_plaat"
```

### Stap 3: Unfold trigger
```python
if classification == "gezette_plaat" and nr_bends > 0:
    # Extract specific solid from assembly
    part_solid = extract_part_solid(bom_item)
    
    # Unfold on solid only (not assembly)
    result = unfold_sheet_metal(
        solid_object=part_solid,  # KEY: Individual solid
        max_bends=nr_bends,
        k_factor=material_kfactor
    )
    
    # Store bend parameters in XML
    if result.success:
        xml.Sheet_BendAngles = ",".join(result.bend_angles)
        xml.Sheet_BendInnerRadii = ",".join(result.bend_radii)
        xml.Sheet_BendLength = ",".join(result.bend_lengths)
        xml.Sheet_UnfoldSuccess = True
```

---

## Validatie-Checklist (v26.03.02)

Gebruik deze checklist om de classificatie te valideren:

### Test Dataset
- [x] **Vlakke plaat:** Silo 2.stp → 17/17 validation PASS
- [x] **Gezette plaat:** 10040852_1.stp (part 10040853_1)
  - [x] Unfold: ✅ SUCCESS
  - [x] Bend angles: ✅ 30°, 30°, 30°
  - [x] Bend radii: ✅ 3mm, 3mm, 3mm
  - [x] Bend lengths: ✅ 1.92mm, 1.92mm, 1.92mm
  - [x] Flatten dims: ✅ 63.93 x 3mm
- [ ] **Profiel:** Testbestand nodig
- [ ] **Anders:** Testbestand nodig

### Implementatie-Checks
- [x] Function signature: `unfold_sheet_metal(solid_object=None, ...)`
- [x] Parameter passing: `_try_unfold(..., solid_object=part_solid)`
- [x] OCP STEP export: Direct `STEPControl_Writer` usage
- [x] JSON serialization: `bend_lines` removed from result dict
- [x] Reference override: Classification overtakes reference if needed

### Edge Cases
- [ ] Assembly met >2 solids (test met 5 solids)
- [ ] Solid extraction correctness (verify per BOM item)
- [ ] Bent sheet in assembly (correctly isolated for unfold)
- [ ] Multiple bent parts in same assembly

---

## Volgende Stappen

1. ✅ Documentatie vastgelegd (dit document)
2. ✅ Unfold gevalideerd voor gezette_plaat
3. ⏳ **Profiel-validatie:** Vraag testbestand aan
4. ⏳ **Edge cases testen:** Assembly-complexiteit opvoeren
5. ⏳ **Classify method update:** Auto-detect van bends in analyse (niet alleen ref-override)

---

## Reference

- **Main Code:** [manufacturing_pipeline/reporting/xml_exporter.py](../manufacturing_pipeline/reporting/xml_exporter.py)
- **Unfold Engine:** [manufacturing_pipeline/analysis/freecad_unfold.py](../manufacturing_pipeline/analysis/freecad_unfold.py)
- **Classification Vars:** [manufacturing_pipeline/analysis/classification_variables.py](../manufacturing_pipeline/analysis/classification_variables.py)
- **Test Results:** [Test output 10040852_1.xml](../../stepfiles/10040852_1_bom_features.xml)

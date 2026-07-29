# Margin Zones Specification

## 1. Purpose

This specification defines how to calculate and plot a **Margin Zone** from a confirmed H4 ZigZag extremum.

The core rule is:

- **FMZ** is the distance from the ZigZag extremum to the **near boundary** of the zone.
- **IMZ** is the distance from the same extremum to the **far boundary** of the zone.
- The **Margin Zone** is the price interval between these two boundaries.
- **MZ** is the width of the zone itself, not the distance from the extremum.

Therefore:

$$
MZ = IMZ - FMZ
$$

A zone built from a local maximum is plotted below the extremum. A zone built from a local minimum is plotted above the extremum.

---

## 2. Input Variables

### Maintenance Margin (MM)

Source:

`CME Group → Markets → FX → G10 → Contract → Margins → Maintenance`

### Pip Price (PP)

Source:

`CME Group → Markets → FX → G10 → Contract → Specs → Minimum Price Fluctuation → CME Globex`

PP must represent the price-increment value used in the calculation for the selected instrument.

### Normalisation Parameter (NP)

NP is used to normalise the calculation to a price step of `0.0001`.

For the instruments covered by this specification:

$$
NP = 2
$$

### Data Review Frequency

Futures contracts normally expire and roll into a new contract every three months. MM may also change during periods of significant market volatility.

MM must therefore be checked:

- at least once per week;
- whenever the active futures contract changes;
- after a significant CME margin update.

Historical MM data:

<https://www.cmegroup.com/solutions/risk-management/margin-services/historical-margins.html#metals>

---

## 3. Distance and Width Calculations

### 3.1 FMZ Distance

FMZ defines the distance from the ZigZag extremum to the near boundary of the Margin Zone:

$$
FMZ = \frac{MM}{PP \times NP}
$$

### 3.2 IMZ Distance

IMZ defines the distance from the same extremum to the far boundary of the Margin Zone:

$$
IMZ = FMZ \times 1.1
$$

IMZ must always be greater than FMZ.

### 3.3 Margin Zone Width

MZ defines only the width of the highlighted interval between the FMZ and IMZ boundaries:

$$
MZ = IMZ - FMZ
$$

MZ must not be used as a synonym for FMZ or for the full distance from the ZigZag extremum.

### 3.4 Zone Percentage Levels

Percentage levels describe a position **inside the Margin Zone**:

- **0% MZ** = FMZ boundary, the near edge of the zone;
- **50% MZ** = midpoint between FMZ and IMZ;
- **100% MZ** = IMZ boundary, the far edge of the zone.

The distance from the extremum to a percentage level $p$ is:

$$
Distance(p) = FMZ + MZ \times p
$$

where $p$ is expressed as a decimal from `0` to `1`.

For the 50% level:

$$
MZ_{50\%} = FMZ + \frac{MZ}{2}
$$

or equivalently:

$$
MZ_{50\%} = \frac{FMZ + IMZ}{2}
$$

---

## 4. Plotting the Zone

Use `ZigZag for H4 Extremums` to identify a confirmed local maximum or local minimum.

Before calculating chart levels, FMZ and IMZ must be converted into the same price units as the chart.

Definitions used below:

- $H$ = price of a confirmed local maximum;
- $L$ = price of a confirmed local minimum;
- $d_{FMZ}$ = FMZ converted into chart price units;
- $d_{IMZ}$ = IMZ converted into chart price units.

### 4.1 Local Maximum

For a local maximum, the Margin Zone is plotted below the extremum.

Near boundary:

$$
FMZ_{level} = H - d_{FMZ}
$$

Far boundary:

$$
IMZ_{level} = H - d_{IMZ}
$$

The highlighted zone is:

$$
[H - d_{IMZ},\ H - d_{FMZ}]
$$

The 50% level is:

$$
MZ_{50\%,level} = H - \frac{d_{FMZ} + d_{IMZ}}{2}
$$

For a local maximum:

- FMZ is the upper and nearer boundary;
- IMZ is the lower and farther boundary;
- MZ is the vertical width between them.

### 4.2 Local Minimum

For a local minimum, the Margin Zone is plotted above the extremum.

Near boundary:

$$
FMZ_{level} = L + d_{FMZ}
$$

Far boundary:

$$
IMZ_{level} = L + d_{IMZ}
$$

The highlighted zone is:

$$
[L + d_{FMZ},\ L + d_{IMZ}]
$$

The 50% level is:

$$
MZ_{50\%,level} = L + \frac{d_{FMZ} + d_{IMZ}}{2}
$$

For a local minimum:

- FMZ is the lower and nearer boundary;
- IMZ is the upper and farther boundary;
- MZ is the vertical width between them.

---

## 5. Calculation Example

Given:

- $MM = 2900$
- $PP = 6.25$
- $NP = 2$

Calculate FMZ:

$$
FMZ = \frac{2900}{6.25 \times 2} = 232
$$

Calculate IMZ:

$$
IMZ = 232 \times 1.1 = 255.2
$$

Calculate the width of the Margin Zone:

$$
MZ = 255.2 - 232 = 23.2
$$

Calculate the midpoint of the zone:

$$
MZ_{50\%} = \frac{232 + 255.2}{2} = 243.6
$$

Interpretation:

- the near boundary is `232` normalised points from the extremum;
- the far boundary is `255.2` normalised points from the extremum;
- the highlighted Margin Zone is `23.2` points wide;
- the 50% level is `243.6` points from the extremum.

Intermediate values must not be rounded. If rounding is required by chart precision, it must be applied only to the final chart price levels.

---

## 6. Implementation Steps

1. Identify the active CME futures contract for the instrument.
2. Retrieve the current MM and PP values.
3. Apply the configured NP value.
4. Calculate FMZ.
5. Calculate IMZ.
6. Calculate the zone width: $MZ = IMZ - FMZ$.
7. Identify a confirmed H4 ZigZag extremum.
8. Determine whether the extremum is a local maximum or local minimum.
9. Convert FMZ and IMZ into chart price units.
10. Calculate the near, far and 50% price levels.
11. Highlight only the interval between the FMZ and IMZ levels.

The horizontal extension, lifetime and invalidation rules of a plotted zone are outside the scope of this specification and must be defined separately by the trading strategy.

---

## 7. Validation Rules

The implementation is correct only when all of the following conditions are met:

1. $FMZ > 0$.
2. $IMZ > FMZ$.
3. $MZ = IMZ - FMZ$.
4. The 50% level is exactly halfway between FMZ and IMZ.
5. A zone created from a local maximum is below that maximum.
6. A zone created from a local minimum is above that minimum.
7. Only the interval between FMZ and IMZ is highlighted.
8. FMZ and IMZ are treated as two boundaries of one zone, not as two separate zones.
9. MZ is treated as the zone width, not as the distance from the ZigZag extremum.

---

## 8. Terminology Summary

| Term | Meaning |
|---|---|
| MM | CME Maintenance Margin |
| PP | Pip or minimum price-increment value used in the calculation |
| NP | Normalisation parameter |
| FMZ | Distance from the ZigZag extremum to the near boundary |
| IMZ | Distance from the ZigZag extremum to the far boundary |
| MZ | Width of the interval between FMZ and IMZ |
| 50% MZ | Midpoint of the zone |
| 100% MZ | Far boundary of the zone, corresponding to IMZ |

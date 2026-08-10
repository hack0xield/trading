# Margin Zones Specification

## 1. Purpose

This specification defines how to calculate and plot a **Margin Zone** from a confirmed H4 ZigZag extremum. It also defines how to plot daily **CFD Rollover Points** that mark the instrument price at the start of each scheduled daily CFD trading break, and how to detect and classify rollover-price crossings of the **50% Extremum-to-50% MZ Level**.

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

### 3.5 50% Extremum-to-50% MZ Level

In addition to the 50% MZ level inside the Margin Zone, the chart must display a separate horizontal level located halfway between the confirmed H4 ZigZag extremum and the **50% MZ** level.

Let $MZ_{50\%}$ be the distance from the ZigZag extremum to the midpoint of the Margin Zone as defined in Section 3.4.

The distance from the ZigZag extremum to this additional horizontal level is:

$$
E_{50\%} = \frac{MZ_{50\%}}{2}
$$

Using the FMZ and IMZ distances directly:

$$
E_{50\%} = \frac{FMZ + IMZ}{4}
$$

Therefore, this level is **not** the 50% level of the Margin Zone itself. It is the midpoint of the distance between the confirmed H4 ZigZag extremum and the existing 50% MZ level.

The level must be plotted as a horizontal price level associated with the same confirmed H4 ZigZag extremum from which the Margin Zone is calculated.

---

## 4. Plotting the Zone

Use `ZigZag for H4 Extremums` to identify a confirmed local maximum or local minimum.

Before calculating chart levels, FMZ and IMZ must be converted into the same price units as the chart.

Definitions used below:

- $H$ = price of a confirmed local maximum;
- $L$ = price of a confirmed local minimum;
- $d_{FMZ}$ = FMZ converted into chart price units;
- $d_{IMZ}$ = IMZ converted into chart price units;
- $d_{MZ50}$ = distance from the extremum to the 50% MZ level in chart price units;
- $d_{E50}$ = distance from the extremum to the additional 50% Extremum-to-50% MZ level in chart price units.

Where:

$$
d_{MZ50} = \frac{d_{FMZ} + d_{IMZ}}{2}
$$

$$
d_{E50} = \frac{d_{MZ50}}{2} = \frac{d_{FMZ} + d_{IMZ}}{4}
$$

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

The 50% MZ level is:

$$
MZ_{50\%,level} = H - \frac{d_{FMZ} + d_{IMZ}}{2}
$$

The additional horizontal level halfway from the confirmed H4 ZigZag maximum to the 50% MZ level is:

$$
E_{50\%,level} = H - \frac{d_{FMZ} + d_{IMZ}}{4}
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

The 50% MZ level is:

$$
MZ_{50\%,level} = L + \frac{d_{FMZ} + d_{IMZ}}{2}
$$

The additional horizontal level halfway from the confirmed H4 ZigZag minimum to the 50% MZ level is:

$$
E_{50\%,level} = L + \frac{d_{FMZ} + d_{IMZ}}{4}
$$

For a local minimum:

- FMZ is the lower and nearer boundary;
- IMZ is the upper and farther boundary;
- MZ is the vertical width between them.

---


## 5. Daily CFD Rollover Price Points

In addition to Margin Zones, the chart must display a point for each trading day that marks the instrument price at the moment the CFD market enters its scheduled daily trading pause / rollover break.

### 5.1 Rollover Time

The **Rollover Time** ($T_{roll}$) is the start time of the broker's scheduled daily trading break for the selected CFD instrument.

The rollover time must be determined from the trading-session schedule of the specific CFD symbol and broker. It must **not** be hardcoded as a universal UTC, local or server time because the daily break can differ between brokers, instruments and trading schedules.

The implementation should use broker/server time internally. If chart time differs from broker/server time, $T_{roll}$ must be converted into the chart timezone before plotting.

For holidays, shortened sessions or other exceptional schedules, the actual scheduled session close for that trading day must be used where this information is available.

### 5.2 Rollover Price

The **Rollover Price** ($P_{roll}$) is the last valid market price available **before** $T_{roll}$.

The price source must be consistent with the price series used by the chart. For example, if the chart is built from Bid prices, the rollover point must use the last Bid price before the trading break.

Formally:

$$
P_{roll}(D) = Price(t^*)
$$

where:

$$
t^* = \max \{t \mid t < T_{roll}(D)\}
$$

and $D$ is the trading day.

The implementation must never use the first price received after the market reopens as the rollover price for the previous session.

If tick data is unavailable and only OHLC bar data is available, use the Close price of the final available bar before $T_{roll}$.

If no valid pre-rollover price is available for a trading day, no rollover point should be plotted for that day. The missing price must not be interpolated.

### 5.3 Plotting the Rollover Point

For every trading day with a scheduled daily CFD break and a valid pre-rollover price, plot one **CFD Rollover Point** with:

- horizontal coordinate: $T_{roll}$;
- vertical coordinate: $P_{roll}$.

The point represents the price at the transition from the active CFD trading session into the daily rollover / maintenance break.

The timestamp of the actual source tick or source bar used to determine $P_{roll}$ should be stored as metadata where possible, even when the visual point itself is positioned at $T_{roll}$.

The rollover point is a standalone marker. It does not create a horizontal level, zone or price range unless such behaviour is defined separately by the trading strategy.

### 5.4 Historical Rollover Points

The same logic must be applied historically so that each completed trading day can have its own rollover point.

For each trading day:

1. determine that day's $T_{roll}$;
2. identify the last valid price before $T_{roll}$;
3. assign that price to $P_{roll}$;
4. plot one point at $(T_{roll}, P_{roll})$.

Days without a scheduled rollover break, days with missing market data, or days on which the required pre-rollover price cannot be determined must not generate an artificial point.


### 5.5 Rollover Crossing Events at the 50% Extremum-to-50% MZ Level

The implementation must detect every event in which two **consecutive valid CFD Rollover Points** are located on opposite sides of the **50% Extremum-to-50% MZ Level** associated with the same confirmed H4 ZigZag extremum.

This is a discrete rollover-to-rollover event. It does **not** mean that the exact intraday time at which market price crossed the horizontal level is known or must be reconstructed.

Let:

- $E_{level}$ = the active 50% Extremum-to-50% MZ price level;
- $P_{roll}(D_{n-1})$ = rollover price for the previous trading day;
- $P_{roll}(D_n)$ = rollover price for the current trading day.

A rollover crossing event exists only when the two rollover prices are strictly on opposite sides of $E_{level}$:

$$
(P_{roll}(D_{n-1}) - E_{level}) \times (P_{roll}(D_n) - E_{level}) < 0
$$

If either rollover price is exactly equal to $E_{level}$, no crossing event is generated for that pair because the requirement that the two points are on opposite sides of the level is not satisfied.

#### 5.5.1 True Crossing

A crossing is classified as **True** when the transition moves the rollover price from the side of $E_{level}$ facing away from the Margin Zone to the side facing the Margin Zone.

For a Margin Zone created from a **local maximum**, the zone is below the extremum and below $E_{level}$. Therefore a True crossing is:

$$
P_{roll}(D_{n-1}) > E_{level}
$$

followed by:

$$
P_{roll}(D_n) < E_{level}
$$

In other words, the rollover price crosses the level **downward**, in the direction of the Margin Zone.

For a Margin Zone created from a **local minimum**, the zone is above the extremum and above $E_{level}$. Therefore a True crossing is:

$$
P_{roll}(D_{n-1}) < E_{level}
$$

followed by:

$$
P_{roll}(D_n) > E_{level}
$$

In other words, the rollover price crosses the level **upward**, in the direction of the Margin Zone.

A True crossing indicates that, based on consecutive daily rollover prices, price has moved across the reference level onto the side that leads toward the associated Margin Zone.

#### 5.5.2 False Crossing

A crossing is classified as **False** when the transition moves the rollover price from the side of $E_{level}$ facing the Margin Zone back to the side facing away from the Margin Zone.

For a Margin Zone created from a **local maximum**, a False crossing is:

$$
P_{roll}(D_{n-1}) < E_{level}
$$

followed by:

$$
P_{roll}(D_n) > E_{level}
$$

The rollover price crosses the level **upward**, away from the Margin Zone.

For a Margin Zone created from a **local minimum**, a False crossing is:

$$
P_{roll}(D_{n-1}) > E_{level}
$$

followed by:

$$
P_{roll}(D_n) < E_{level}
$$

The rollover price crosses the level **downward**, away from the Margin Zone.

#### 5.5.3 Event Timestamp and Plotting

The crossing event must be assigned to the **second rollover point** in the pair, because the event becomes confirmed only when $P_{roll}(D_n)$ is known.

For each detected crossing event, store at minimum:

- the active Margin Zone / ZigZag extremum identifier;
- the extremum type: local maximum or local minimum;
- previous rollover timestamp and price;
- current rollover timestamp and price;
- $E_{level}$ used for the comparison;
- crossing direction: upward or downward;
- classification: `True` or `False`.

On the chart, the second CFD Rollover Point in the pair must receive an additional event marker or label indicating `True` or `False`. The normal CFD Rollover Point itself must remain available as the underlying daily price marker.

The visual style of True and False markers is configurable and is outside the calculation logic, but the two classifications must be visually distinguishable.

#### 5.5.4 Consecutive-Day and Level-Consistency Rules

A crossing may be detected only when both rollover points belong to consecutive scheduled trading days for which valid rollover prices are available.

If an intervening scheduled trading day has no valid rollover point, the implementation must **not** infer a crossing across that data gap.

Both rollover points must also be evaluated against the **same active Margin Zone instance and the same $E_{level}$**.

If a new confirmed H4 ZigZag extremum creates a new Margin Zone, or if the active $E_{level}$ changes because the zone is recalculated, the crossing state must be reset. The first rollover point associated with the new or recalculated level establishes a new baseline and cannot itself generate a crossing event against a rollover point from the previous level configuration.

This prevents artificial True or False events caused solely by a change in the reference level rather than by rollover-price movement across a stable level.

---

## 6. Calculation Example

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

Calculate the additional level halfway from the ZigZag extremum to the 50% MZ level:

$$
E_{50\%} = \frac{243.6}{2} = 121.8
$$

Interpretation:

- the near boundary is `232` normalised points from the extremum;
- the far boundary is `255.2` normalised points from the extremum;
- the highlighted Margin Zone is `23.2` points wide;
- the 50% MZ level is `243.6` points from the extremum;
- the additional 50% Extremum-to-50% MZ level is `121.8` points from the extremum.

Intermediate values must not be rounded. If rounding is required by chart precision, it must be applied only to the final chart price levels.

---

## 7. Implementation Steps

1. Identify the active CME futures contract for the instrument.
2. Retrieve the current MM and PP values.
3. Apply the configured NP value.
4. Calculate FMZ.
5. Calculate IMZ.
6. Calculate the zone width: $MZ = IMZ - FMZ$.
7. Identify a confirmed H4 ZigZag extremum.
8. Determine whether the extremum is a local maximum or local minimum.
9. Convert FMZ and IMZ into chart price units.
10. Calculate the near, far and 50% MZ price levels.
11. Calculate and plot the additional horizontal level halfway between the confirmed H4 ZigZag extremum and the 50% MZ price level.
12. Highlight only the interval between the FMZ and IMZ levels.
13. Determine the scheduled daily CFD rollover break start time for the selected symbol and broker.
14. For each trading day, retrieve the last valid chart-consistent price before the rollover break.
15. Plot one CFD Rollover Point at the rollover time using that pre-break price.
16. Skip the point if no valid pre-rollover price is available; do not interpolate missing values.
17. For each pair of consecutive valid rollover points associated with the same active Margin Zone and unchanged $E_{level}$, compare their positions relative to the 50% Extremum-to-50% MZ Level.
18. If the two rollover points are strictly on opposite sides of $E_{level}$, create a rollover crossing event on the second point.
19. Classify the event as `True` when the crossing moves toward the Margin Zone and as `False` when the crossing moves away from the Margin Zone.
20. Reset the crossing baseline whenever the active ZigZag extremum, Margin Zone instance or $E_{level}$ changes, and do not infer crossings across missing rollover days.

The horizontal extension, lifetime and invalidation rules of a plotted zone are outside the scope of this specification and must be defined separately by the trading strategy.

---

## 8. Validation Rules

The implementation is correct only when all of the following conditions are met:

1. $FMZ > 0$.
2. $IMZ > FMZ$.
3. $MZ = IMZ - FMZ$.
4. The 50% MZ level is exactly halfway between FMZ and IMZ.
5. The 50% Extremum-to-50% MZ level is exactly halfway between the confirmed H4 ZigZag extremum and the 50% MZ level.
6. A zone created from a local maximum is below that maximum.
7. A zone created from a local minimum is above that minimum.
8. Only the interval between FMZ and IMZ is highlighted.
9. FMZ and IMZ are treated as two boundaries of one zone, not as two separate zones.
10. MZ is treated as the zone width, not as the distance from the ZigZag extremum.
11. A CFD Rollover Point uses the last valid market price strictly before the scheduled daily trading break.
12. The first price after reopening is never used as the previous session's rollover price.
13. At most one rollover point is plotted for each scheduled daily break.
14. Missing pre-rollover prices are not interpolated.
15. The plotted rollover timestamp corresponds to the scheduled break start for that symbol and broker.
16. A rollover crossing event is generated only when two consecutive valid rollover points are strictly on opposite sides of the same unchanged $E_{level}$.
17. A rollover price exactly equal to $E_{level}$ does not by itself generate a crossing event.
18. For a local maximum, downward crossings are `True` and upward crossings are `False`.
19. For a local minimum, upward crossings are `True` and downward crossings are `False`.
20. The crossing event is timestamped and plotted on the second rollover point in the qualifying pair.
21. No crossing is inferred across a missing scheduled rollover point.
22. Changing the active ZigZag extremum, Margin Zone instance or $E_{level}$ resets the crossing baseline.

---

## 9. Terminology Summary

| Term | Meaning |
|---|---|
| MM | CME Maintenance Margin |
| PP | Pip or minimum price-increment value used in the calculation |
| NP | Normalisation parameter |
| FMZ | Distance from the ZigZag extremum to the near boundary |
| IMZ | Distance from the ZigZag extremum to the far boundary |
| MZ | Width of the interval between FMZ and IMZ |
| 50% MZ | Midpoint of the zone |
| $E_{50\%}$ | Distance from the confirmed H4 ZigZag extremum to the horizontal level halfway between the extremum and 50% MZ |
| 50% Extremum-to-50% MZ Level | Horizontal price level located halfway between the confirmed H4 ZigZag extremum and the 50% MZ level |
| 100% MZ | Far boundary of the zone, corresponding to IMZ |
| $T_{roll}$ | Start time of the scheduled daily CFD trading break |
| $P_{roll}$ | Last valid chart-consistent market price before $T_{roll}$ |
| CFD Rollover Point | Daily chart marker plotted at $(T_{roll}, P_{roll})$ |
| Rollover Crossing Event | Event where two consecutive valid CFD Rollover Points lie on opposite sides of the same 50% Extremum-to-50% MZ Level |
| True Crossing | Rollover crossing that moves from the side away from the Margin Zone to the side facing the Margin Zone |
| False Crossing | Rollover crossing that moves from the side facing the Margin Zone to the side away from the Margin Zone |

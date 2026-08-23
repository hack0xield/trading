
## Data Variables


1) Maintenance Margin = MM = https://www.cmegroup.com/ -> markets -> FX->G10-> contract-> margins-> maintenance
	1) [Euro FX Futures Margins - CME Group](https://www.cmegroup.com/markets/fx/g10/euro-fx.margins.html)
2) Pips Price = PP =  https://www.cmegroup.com/ -> markets -> FX->G10-> contract-> specs -> minimum price fluctuation -> CME Globex per euro increment
3) [FX Product Guide 2026 - CME Group](https://www.cmegroup.com/markets/fx/fx-product-guide.html)
4) Normalization parameter for 0.00010 pips = NP (always = 2)


Every 3 months futures contracts expire and are rolled into the new one. MM can change upon expiration OR it can change in case of significant price volatility. It makes esnse to check MM for consistency every week. 

Historical MM data: 

[https://www.cmegroup.com/solutions/risk-management/margin-services/historical-margins.html#metals]()


## Formulas


1) To build Forward Margin Zone (MZ) we will use the following formula: 

$$
FMZ = MM/(PP*NP)
$$
2) To build Initial Margin Zone (IMZ) we will use the following formula: 

$$
IMZ = MZ *1.1
$$

> Calc example:
> ММ 2900
> PP 6,25
> NP = 2
> 
> FMZ = 2900/12,5 = 232
> IMZ = 255


## Margin Ranges


For building trading strategies we will be using Margin Ranges (PR) between FMZ and IMZ: 

$$
MR = IMZ - FMZ
$$

Steps for applying MR to a price chart: 

1) Use [[ZigZag for H4 Extremums]] for local min/max identification
2) Use the min/max price point to build 50% MR and 100% MR


## Practical execution steps

1) Get EUR/USD chart data for the recent 3 years
2) Use H4 timeframe
3) Apply ZigZag to identify and visualise local extremums min/max
4) Use historical Margins data to calc margin zone FMZ and IMZ [https://www.cmegroup.com/solutions/risk-management/margin-services/historical-margins.html#metals]()
5) Draw [FMZ, IMZ] envelopes on chart, using every H4 min/max as checkpoint to re-calc the next  [FMZ, IMZ] range  


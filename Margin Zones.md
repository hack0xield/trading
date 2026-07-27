
## Data Variables


1) Maintenance Margin = MM = https://www.cmegroup.com/ -> markets -> FX->G10-> contract-> margins-> maintenance
2) Pips Price = PP =  https://www.cmegroup.com/ -> markets -> FX->G10-> contract-> specs -> minimum price fluctuation -> CME Globex per euro increment
3) Normalization parameter for 0.0001 pips = NP (always = 2)


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

## Example


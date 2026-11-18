You are a single bacterial species acting inside a metabolic simulation: Prevotella copri CB7 DSM 18205.

You are not an assistant and you are not describing an organism from the outside. You *are* this organism. Every turn you sense your immediate chemical surroundings and commit to a metabolic strategy: what you take up, what you release, and how fast you grow.


## Rules of the world
- Mass is conserved. You cannot release atoms you did not take in. Carbon, nitrogen, phosphorus and sulfur are all audited.
- You can only use transporters your genome encodes.
- Growth costs carbon and nitrogen. Roughly 45 mmol of carbon and 10 mmol of nitrogen are needed per gram of dry weight produced.
- Fluxes are in mmol/gDW/h. Realistic uptake rates are 0-20; growth rates are 0-1.5 /h.
- If nothing you can metabolise is present, take up nothing and report zero growth. Claiming growth you cannot support will be detected and corrected.

Think it through before you answer. Your reply ends with a single JSON object.

## The vessel
This is a **sealed batch culture**: 1 mL, anaerobic, 37 C, buffered at pH 7.
Nothing flows in and nothing flows out. Whatever is listed above is the entire
supply for the whole experiment — it is not replenished, and anything you release
stays in the vessel and accumulates. The culture runs for 48 h in total.

# Environment report — turn 7, t = 28.00 h

You are pcopri, at 2.202 gDW/L in the 'flask' compartment.

## Available nutrients (mmol/L)
- Carbon dioxide (co2): 47.62
- Acetate (ac): 30.21
- Propionate (ppa): 13.13
- L-cysteine (cys__L): 8.4
- myo-inositol (inost): 6.272
- L-proline (pro__L): 5.9
- Glycine (gly): 4.7
- L-phenylalanine (phe__L): 4.5
- Maltose (malt): 4.382
- L-leucine (leu__L): 3.6
- L-tyrosine (tyr__L): 3.201
- L-valine (val__L): 2.8
- L-lysine (lys__L): 2.4
- L-arabinose (arab__L): 2.224
- L-threonine (thr__L): 1.9
- L-asparagine (asn__L): 1.802
- L-isoleucine (ile__L): 1.6
- Succinate (succ): 1.51
- L-alanine (ala__L): 1.405
- D-glucose (glc__D): 1.2
- L-glutamine (gln__L): 1.103
- L-histidine (his__L): 1
- L-methionine (met__L): 0.84
- L-tryptophan (trp__L): 0.73
- L-glutamate (glu__L): 0.6627
- L-aspartate (asp__L): 0.4
- Nitrate (no3): 0.1093
- 4-Aminobenzoate (4abz): 0.07307
- also present, each below 0.073 mM: Orotate (orot), Biotin (btn), Nicotinamide (ncam), Xanthine (xan), Pyridoxamine (pydam), Thymidine (thymd), Hydrogen (h2), Protoheme (pheme), Pyridoxal (pydx), Folate (fol), Pyridoxine (pydxn), Thiamin (thm), Riboflavin (ribflv), (R)-Pantothenate (pnto__R), Adenine (ade), Guanine (gua), cytosine (csn), uracil (ura), 5,6,7,8-Tetrahydrofolate (thf), Cob(I)alamin (cbl1)
Present in excess, never limiting: calcium(2+) (ca2), Chloride (cl), Co2+ (cobalt2), Cu2+ (cu2), Fe2+ (fe2), Fe3+ (fe3), potassium (k), magnesium (mg2), Mn2+ (mn2), Molybdate (mobd), Sodium (na1), Ni2+ (ni2), hydrogenphosphate (pi), Selenate (sel), Selenite (slnt), sulfate (so4), Tungstate (tungs), Zinc (zn2). Water is unlimited.

## Conditions
- pH: 7.00
- temperature: 37 C
- redox potential: -200 mV
- dissolved oxygen: 0 mmol/L (anaerobic)

## Neighbours
- none detected; you are growing in isolation

## Your recent behaviour
- turn 3: growth 0.550 /h; L-lactate (lac__L) -10.00, L-arginine (arg__L) -5.00, Acetate (ac) +5.00, Propionate (ppa) +2.50, Ammonium (nh4) -2.00, hydrogenphosphate (pi) -0.73, sulfate (so4) -0.14
- turn 4: growth 0.160 /h; L-arabinose (arab__L) -10.00, L-serine (ser__L) -5.00, Acetate (ac) +5.00, Propionate (ppa) +2.00, Succinate (succ) +1.00, Ammonium (nh4) -0.85, hydrogenphosphate (pi) -0.22
- turn 5: growth 0.000 /h; L-arabinose (arab__L) -10.00, L-alanine (ala__L) -5.00, Acetate (ac) +5.00, L-serine (ser__L) -3.52, Propionate (ppa) +2.00, Succinate (succ) +1.00, sulfate (so4) -0.26
- turn 6: growth 0.300 /h; L-lactate (lac__L) -10.00, L-arabinose (arab__L) -5.00, Acetate (ac) +5.00, L-glutamine (gln__L) -2.00, Propionate (ppa) +2.00, L-asparagine (asn__L) -1.00, hydrogenphosphate (pi) -0.41, sulfate (so4) -0.08

## Your decision
Choose the exchange fluxes you will run for the next 4.00 h, in mmol per gram dry weight per hour.
Sign convention: NEGATIVE = uptake (you consume it), POSITIVE = secretion (you release it).
Work through the decision in prose first, at whatever length you need:
- which carbon and nitrogen sources you can actually transport, and which abundant ones you cannot use, and why
- which fermentation products this species is known to make
- the growth rate your chosen uptake supports; derive it, do not pick a round default
- an audit: carbon in must cover carbon out plus the biomass cost above

Then, as the LAST thing in your reply and with nothing after it, emit a single JSON object:
{"fluxes": {"<metabolite_id>": <float>, ...}, "growth_rate": <float, 1/h>, "rationale": "<why, briefly>"}

Also include `"confidence": <0.0-1.0>` in the same object.

For anything you take **up**, use the ids exactly as they appear above — the id is
the part in parentheses. For anything you **release**, you may use any standard
metabolite id even if it does not appear above, since a product need not already
be present. The usual ones are: `ac` acetate, `but` butyrate, `ppa` propionate,
`lac__L` and `lac__D` lactate, `for` formate, `succ` succinate, `etoh` ethanol,
`co2`, `h2`, `h2s`, `nh4`. Listing one here says nothing about whether you can
make it; that is your decision.

Mass is audited: you cannot release more carbon than you take in, and growth
consumes carbon and nitrogen on top of that.
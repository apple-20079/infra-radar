# infra-radar

Robot de veille d'offres (banques, boutiques M&A, PE, private credit, fonds infra, souverains) pour Hong Kong, Londres, New York et Paris.

- `robot/firms.json` : la liste des acteurs suivis (tier 1 = suivi quotidien, tier 2 = une fois par semaine).
- `robot/radar.py` : interroge chaque matin les sites de recrutement des employeurs (Workday, Greenhouse, Lever, Ashby, SmartRecruiters, Oracle) et le site officiel des VIE.
- `data/jobs.json` : les offres retenues (lien direct employeur, date de publication, extrait).
- `data/status.json` : ce qui a marché ou non pour chaque acteur.
- `data/discovery.json` : les sources trouvées automatiquement.

Seules des adresses publiques sont interrogées, une fois par jour, avec des requêtes espacées.

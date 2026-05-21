import requests
import random
import re

SINGAPORE_PLACE_ID = ""

# Each group maps to its iNaturalist taxon_id.
# Giving under-represented groups more slots in the wheel.
TAXON_GROUPS = [
    {"name": "Echinodermata", "taxon_id": 47549,  "weight": 12},
    {"name": "Platyhelminthes", "taxon_id": 52319,  "weight": 10},
    {"name": "Onychophora", "taxon_id": 51836,  "weight": 8},
    {"name": "Porifera",      "taxon_id": 48824,  "weight": 6},
    {"name": "Crustacea",     "taxon_id": 85493,  "weight": 4},
    {"name": "Mollusca",      "taxon_id": 47115,  "weight": 6},
    {"name": "Arachnida",     "taxon_id": 47119,  "weight": 3},
    {"name": "Cnidaria",      "taxon_id": 47534,  "weight": 12},
    {"name": "Annelida",      "taxon_id": 47491,  "weight": 10},
    {"name": "Insecta",       "taxon_id": 47158,  "weight": 2},
    {"name": "Nematoda",       "taxon_id": 54960,  "weight": 8},
    {"name": "Vertebrata",     "taxon_id": 355675,  "weight": 6},
    {"name": "Tunicata",      "taxon_id": 130868, "weight": 8},
]

def get_taxonomy(taxon_id):
    url  = f"https://api.inaturalist.org/v1/taxa/{taxon_id}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not data["results"]:
        return {}
    taxon    = data["results"][0]
    taxonomy = {a["rank"]: a["name"] for a in taxon.get("ancestors", [])}
    taxonomy["species"] = taxon.get("name", "")
    return taxonomy


def get_wikipedia_extras(scientific_name, wikipedia_url=""):
    """
    Strategy:
    1. Resolve the Wikipedia page title (from wikipedia_url slug or search)
    2. Fetch the section list for that page
    3. Find sections matching 'habitat' and 'feeding' (case-insensitive)
    4. Fetch and clean those specific sections' wikitext
    5. Fall back to REST summary extract if no matching sections found
    """

    # ── STEP 1: resolve page title ────────────────────────────────────
    title = None

    if wikipedia_url:
        slug = wikipedia_url.rstrip("/").split("/")[-1]
        if slug:
            title = slug.replace("_", " ")

    if not title:
        try:
            r = requests.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action":   "query",
                    "list":     "search",
                    "srsearch": scientific_name,
                    "srlimit":  1,
                    "format":   "json",
                },
                headers={"User-Agent": "SGAnimalExplorer/1.0"},
                timeout=8
            )
            results = r.json().get("query", {}).get("search", [])
            if results:
                title = results[0]["title"]
        except Exception:
            return {"habitat": "", "feeding": ""}

    if not title:
        return {"habitat": "", "feeding": ""}

    # ── STEP 2: fetch section list ────────────────────────────────────
    try:
        r = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "parse",
                "page":   title,
                "prop":   "sections",
                "format": "json",
            },
            headers={"User-Agent": "SGAnimalExplorer/1.0"},
            timeout=8
        )
        sections = r.json().get("parse", {}).get("sections", [])
    except Exception:
        sections = []

    # ── STEP 3: match sections by heading ────────────────────────────
    # Ordered from most-specific to least-specific.
    # The code will prefer an exact match higher up the preference list
    # over whatever appears first in the Wikipedia section order.
    habitat_preference = [
        "habitat",
        "habitat and range",
        "ecology and habitat",
        "habitat and ecology",
        "ecology",
        "distribution and habitat",
        "habitat and distribution",
        "distribution",
        "range",
        "range and habitat",
    ]
    feeding_preference = [
        "feeding",
        "diet",
        "feeding and diet",
        "diet and feeding",
        "feeding behaviour",
        "feeding behavior",
        "food",
        "predation",
        "foraging",
    ]

    def best_section_index(sections, preference_list):
        """
        Score every section against the preference list.
        Lower index in preference_list = higher priority.
        Returns the section index string of the best match, or None.
        """
        best_priority = None
        best_index    = None

        for sec in sections:
            heading  = sec.get("line",   "").lower().strip()
            anchor   = sec.get("anchor", "").lower().strip()

            for priority, pref in enumerate(preference_list):
                # Only accept if it's an exact match on heading or anchor
                if pref == heading or pref == anchor:
                    if best_priority is None or priority < best_priority:
                        best_priority = priority
                        best_index    = sec.get("index")
                    break   # no need to check lower priorities for this section

        return best_index

    habitat_index = best_section_index(sections, habitat_preference)
    feeding_index = best_section_index(sections, feeding_preference)

    # ── STEP 4: fetch each matched section ────────────────────────────
    def fetch_section(page_title, section_index):
        try:
            r = requests.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action":  "parse",
                    "page":    page_title,
                    "section": section_index,
                    "prop":    "wikitext",
                    "format":  "json",
                },
                headers={"User-Agent": "SGAnimalExplorer/1.0"},
                timeout=8
            )
            wikitext = r.json().get("parse", {}).get("wikitext", {}).get("*", "")
            return first_sentences(clean_wikitext(wikitext), 3)
        except Exception:
            return ""

    habitat_text = fetch_section(title, habitat_index) if habitat_index else ""
    feeding_text = fetch_section(title, feeding_index) if feeding_index else ""

    # ── STEP 5: fall back to REST summary if either still empty ───────
    if not habitat_text or not feeding_text:
        try:
            r = requests.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{title.replace(' ', '_')}",
                headers={"User-Agent": "SGAnimalExplorer/1.0"},
                timeout=8
            )
            if r.status_code == 200:
                extract   = r.json().get("extract", "").strip()
                sentences = re.split(r"(?<=[.!?])\s+", extract)

                habitat_keywords = [
                    "habitat", "found in", "lives in", "inhabit", "distributed",
                    "occurs in", "native to", "reef", "forest", "mangrove",
                    "coastal", "freshwater", "marine", "terrestrial",
                    "seabed", "sandy", "muddy", "rocky", "intertidal",
                    "lagoon", "estuary", "depth", "shallow", "benthic",
                ]
                feeding_keywords = [
                    "feed", "feeds", "diet", "prey", "eat", "carnivore", "herbivore",
                    "omnivore", "forage", "hunt", "predator", "scavenge",
                    "consume", "filter", "graze", "detritivore", "algae",
                ]

                if not habitat_text:
                    matched = [s for s in sentences if any(k in s.lower() for k in habitat_keywords)]
                    habitat_text = " ".join(matched[:2]) if matched else " ".join(sentences[:2])

                if not feeding_text:
                    matched = [s for s in sentences if any(k in s.lower() for k in feeding_keywords)]
                    feeding_text = " ".join(matched[:2]) if matched else " ".join(sentences[:2])

        except Exception:
            pass

    return {"habitat": habitat_text, "feeding": feeding_text}

def clean_wikitext(text):
    """Strip wikitext markup to plain readable text."""
    text = re.sub(r"\{\{[^}]*\}\}", "", text)        # remove templates {{...}}
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", text)  # [[link|label]] → label
    text = re.sub(r"'{2,}", "", text)                # remove bold/italic ''
    text = re.sub(r"==+[^=]+=+", "", text)           # remove headings
    text = re.sub(r"<[^>]+>", "", text)              # remove HTML tags
    text = re.sub(r"\[\d+\]", "", text)              # remove citation [1]
    text = re.sub(r"\n+", " ", text).strip()
    return text


def first_sentences(text, n=2):
    """Return the first n sentences of a block of text."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(sentences[:n]).strip()


def build_observation(obs):
    taxon = obs.get("taxon")
    if not taxon:
        return None

    photos       = obs.get("photos", [])
    image_url    = photos[0].get("url", "").replace("square", "medium") if photos else ""
    taxonomy     = get_taxonomy(taxon["id"])
    sci_name     = taxon.get("name", "")
    wikipedia_url = taxon.get("wikipedia_url", "")
    extras       = get_wikipedia_extras(sci_name, wikipedia_url)  # ← pass url

    return {
        "id":              taxon.get("id"),
        "common_name":     taxon.get("preferred_common_name") or sci_name or "Unknown",
        "scientific_name": sci_name,
        "phylum":          taxonomy.get("phylum", "—"),
        "class_":          taxonomy.get("class",  "—"),
        "order_":          taxonomy.get("order",  "—"),
        "family":          taxonomy.get("family", "—"),
        "genus":           taxonomy.get("genus",  "—"),
        "image_url":       image_url,
        "wikipedia_url":   wikipedia_url,
        "habitat":         extras["habitat"],
        "feeding":         extras["feeding"],
    }
def fetch_singapore_animals(query="", page=1, per_page=20, phyla_filter=None):
    params = {
        "place_id":     SINGAPORE_PLACE_ID,
        "taxon_id":     1,  # Animalia
        "native":       "true",
        "photos":       "true",
        "quality_grade":"research",
        "per_page":     per_page,
        "page":         page,
        "order":        "desc",
        "order_by":     "votes",
    }
    if query:
        params["taxon_name"] = query

    url  = "https://api.inaturalist.org/v1/observations"
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    seen_taxa = set()
    results   = []

    for obs in data.get("results", []):
        taxon = obs.get("taxon")
        if not taxon:
            continue
        taxon_id = taxon.get("id")
        if taxon_id in seen_taxa:
            continue
        seen_taxa.add(taxon_id)
        entry = build_observation(obs)
        if entry:
            results.append(entry)

    if phyla_filter:
        results = [r for r in results if r["phylum"] in phyla_filter]

    return results, data.get("total_results", 0)


def fetch_random_animal(phyla_filter=None):
    # Build weighted pool
    pool = []
    for group in TAXON_GROUPS:
        pool.extend([group] * group["weight"])

    if phyla_filter:
        filtered_pool = [g for g in pool if g["name"] in phyla_filter]
        if filtered_pool:
            pool = filtered_pool

    for _ in range(15):
        group = random.choice(pool)

        # Step 1: find out how many observations exist for this group
        count_resp = requests.get(
            "https://api.inaturalist.org/v1/observations",
            params={
                "place_id":      SINGAPORE_PLACE_ID,
                "taxon_id":      group["taxon_id"],
                "native":        "true",
                "photos":        "true",
                "quality_grade": "research",
                "per_page":      0,   # we only want the total count
            },
            timeout=10
        )
        total = count_resp.json().get("total_results", 200)
        # iNaturalist caps at page 100 with per_page 200 = 20,000 max
        per_page  = 200
        max_page  = min(100, max(1, total // per_page))
        rand_page = random.randint(1, max_page)

        # Step 2: fetch a page of results and pick one randomly
        resp = requests.get(
            "https://api.inaturalist.org/v1/observations",
            params={
                "place_id":      SINGAPORE_PLACE_ID,
                "taxon_id":      group["taxon_id"],
                "native":        "true",
                "photos":        "true",
                "quality_grade": "research",
                "per_page":      per_page,
                "page":          rand_page,
                "order":         "random",
            },
            timeout=10
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            continue

        # Pick a random obs from the returned page for extra variety
        obs   = random.choice(results)
        entry = build_observation(obs)
        if not entry:
            continue
        if phyla_filter and entry["name"] not in phyla_filter:
            continue

        return entry

    return None
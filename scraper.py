import requests
import random
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

SINGAPORE_PLACE_ID = ""

# Each group maps to its iNaturalist taxon_id.
# Giving under-represented groups more slots in the wheel.
TAXON_GROUPS = [
    {"name": "Echinodermata",    "taxon_id": 47549,  "weight": 12, "phylum": "Echinodermata"},
    {"name": "Platyhelminthes",  "taxon_id": 52319,  "weight": 10, "phylum": "Platyhelminthes"},
    {"name": "Porifera",         "taxon_id": 48824,  "weight": 6,  "phylum": "Porifera"},
    {"name": "Crustacea",        "taxon_id": 85493,  "weight": 8,  "phylum": "Arthropoda"},
    {"name": "Brachyura",        "taxon_id": 121639,  "weight": 8,  "phylum": "Arthropoda"},
    {"name": "Mollusca",         "taxon_id": 47115,  "weight": 8,  "phylum": "Mollusca"},
    {"name": "Gastropoda",       "taxon_id": 47114,  "weight": 6,  "phylum": "Mollusca"},
    {"name": "Cephalopoda",      "taxon_id": 47459,  "weight": 7,  "phylum": "Mollusca"},
    {"name": "Bivalvia",         "taxon_id": 47584,  "weight": 5,  "phylum": "Mollusca"},
    {"name": "Arachnida",        "taxon_id": 47119,  "weight": 5,  "phylum": "Arthropoda"},
    {"name": "Cnidaria",         "taxon_id": 47534,  "weight": 12, "phylum": "Cnidaria"},
    {"name": "Anthozoa",         "taxon_id": 47533,  "weight": 8,  "phylum": "Cnidaria"},
    {"name": "Scyphozoa",        "taxon_id": 48332,  "weight": 6,  "phylum": "Cnidaria"},
    {"name": "Annelida",         "taxon_id": 47491,  "weight": 10, "phylum": "Annelida"},
    {"name": "Polychaeta",       "taxon_id": 47490,  "weight": 8,  "phylum": "Annelida"},
    {"name": "Holothuroidea",    "taxon_id": 47720,  "weight": 8,  "phylum": "Echinodermata"},
    {"name": "Asteroidea",       "taxon_id": 47668,  "weight": 8,  "phylum": "Echinodermata"},
    {"name": "Echinoidea",       "taxon_id": 47548,  "weight": 6,  "phylum": "Echinodermata"},
    {"name": "Ophiuroidea",      "taxon_id": 48836,  "weight": 6,  "phylum": "Echinodermata"},
    {"name": "Tunicata",         "taxon_id": 130868, "weight": 6,  "phylum": "Chordata"},
    {"name": "Onychophora",         "taxon_id": 51836, "weight": 8,  "phylum": "Chordata"},
    {"name": "Insecta",          "taxon_id": 47158,  "weight": 2,  "phylum": "Arthropoda"},
    {"name": "Nematoda",          "taxon_id": 54960,  "weight": 8,  "phylum": "Nematoda"},
    {"name": "Vertebrata",       "taxon_id": 355675, "weight": 2,  "phylum": "Chordata"},
    {"name": "Plantae",       "taxon_id": 47126, "weight": 12,  "phylum": "Plantae"},
]

# ── TAXONOMY (cached) ────────────────────────────────────────────────────────
# lru_cache means repeated species never hit the API twice in one session
@lru_cache(maxsize=512)
def get_taxonomy(taxon_id):
    try:
        resp = requests.get(
            f"https://api.inaturalist.org/v1/taxa/{taxon_id}",
            timeout=8
        )
        resp.raise_for_status()
        data = resp.json()
        if not data["results"]:
            return {}
        taxon    = data["results"][0]
        taxonomy = {a["rank"]: a["name"] for a in taxon.get("ancestors", [])}
        taxonomy["species"] = taxon.get("name", "")
        return taxonomy
    except Exception:
        return {}


# ── WIKIPEDIA ────────────────────────────────────────────────────────────────
def get_wikipedia_extras(scientific_name, wikipedia_url=""):
    """
    Parallel fetch of habitat + feeding sections.
    Step 1: resolve title
    Step 2: fetch section list
    Step 3: fetch habitat section + feeding section in parallel
    Step 4: fall back to REST summary if either is still empty
    """
    title = None
    if wikipedia_url:
        slug = wikipedia_url.rstrip("/").split("/")[-1]
        if slug:
            title = slug.replace("_", " ")

    if not title:
        try:
            r = requests.get(
                "https://en.wikipedia.org/w/api.php",
                params={"action": "query", "list": "search",
                        "srsearch": scientific_name, "srlimit": 1, "format": "json"},
                headers={"User-Agent": "SGAnimalExplorer/1.0"},
                timeout=6
            )
            results = r.json().get("query", {}).get("search", [])
            if results:
                title = results[0]["title"]
        except Exception:
            return {"habitat": "", "feeding": ""}

    if not title:
        return {"habitat": "", "feeding": ""}

    # Fetch section list
    try:
        r = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "parse", "page": title, "prop": "sections", "format": "json"},
            headers={"User-Agent": "SGAnimalExplorer/1.0"},
            timeout=6
        )
        sections = r.json().get("parse", {}).get("sections", [])
    except Exception:
        sections = []

    habitat_preference = [
        "habitat", "habitat and range", "ecology and habitat", "habitat and ecology",
        "ecology", "distribution and habitat", "habitat and distribution",
        "distribution", "range", "range and habitat",
    ]
    feeding_preference = [
        "feeding", "diet", "feeding and diet", "diet and feeding",
        "feeding behaviour", "feeding behavior", "food", "predation", "foraging",
    ]

    def best_section_index(secs, prefs):
        best_priority, best_index = None, None
        for sec in secs:
            heading = sec.get("line", "").lower().strip()
            anchor  = sec.get("anchor", "").lower().strip()
            for priority, pref in enumerate(prefs):
                if pref == heading or pref == anchor:
                    if best_priority is None or priority < best_priority:
                        best_priority = priority
                        best_index    = sec.get("index")
                    break
        return best_index

    habitat_index = best_section_index(sections, habitat_preference)
    feeding_index = best_section_index(sections, feeding_preference)

    def fetch_section(section_index):
        if not section_index:
            return ""
        try:
            r = requests.get(
                "https://en.wikipedia.org/w/api.php",
                params={"action": "parse", "page": title, "section": section_index,
                        "prop": "wikitext", "format": "json"},
                headers={"User-Agent": "SGAnimalExplorer/1.0"},
                timeout=6
            )
            wikitext = r.json().get("parse", {}).get("wikitext", {}).get("*", "")
            return first_sentences(clean_wikitext(wikitext), 3)
        except Exception:
            return ""

    # ── Fetch habitat + feeding sections IN PARALLEL ─────────────────
    habitat_text = ""
    feeding_text = ""
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_habitat = ex.submit(fetch_section, habitat_index)
        fut_feeding = ex.submit(fetch_section, feeding_index)
        habitat_text = fut_habitat.result()
        feeding_text = fut_feeding.result()

    # Fall back to REST summary for anything still empty
    if not habitat_text or not feeding_text:
        try:
            r = requests.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{title.replace(' ', '_')}",
                headers={"User-Agent": "SGAnimalExplorer/1.0"},
                timeout=6
            )
            if r.status_code == 200:
                extract   = r.json().get("extract", "").strip()
                sentences = re.split(r"(?<=[.!?])\s+", extract)

                habitat_kw = ["habitat", "found in", "lives in", "inhabit", "distributed",
                              "occurs in", "native to", "reef", "forest", "mangrove",
                              "coastal", "freshwater", "marine", "terrestrial", "intertidal",
                              "lagoon", "estuary", "depth", "shallow", "benthic"]
                feeding_kw = ["feed", "feeds", "diet", "prey", "eat", "carnivore",
                              "herbivore", "omnivore", "forage", "hunt", "predator",
                              "scavenge", "consume", "filter", "graze", "detritivore"]

                if not habitat_text:
                    matched = [s for s in sentences if any(k in s.lower() for k in habitat_kw)]
                    habitat_text = " ".join(matched[:2]) if matched else " ".join(sentences[:2])
                if not feeding_text:
                    matched = [s for s in sentences if any(k in s.lower() for k in feeding_kw)]
                    feeding_text = " ".join(matched[:2])
        except Exception:
            pass

    return {"habitat": habitat_text, "feeding": feeding_text}


# ── WIKITEXT HELPERS ─────────────────────────────────────────────────────────
def clean_wikitext(text):
    text = re.sub(r"\{\{[^}]*\}\}", "", text)
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"'{2,}", "", text)
    text = re.sub(r"==+[^=]+=+", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\[\d+\]", "", text)
    text = re.sub(r"\n+", " ", text).strip()
    return text

def first_sentences(text, n=2):
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(sentences[:n]).strip()


# ── BUILD OBSERVATION ────────────────────────────────────────────────────────
def build_observation(obs):
    taxon = obs.get("taxon")
    if not taxon:
        return None

    photos        = obs.get("photos", [])
    image_url     = photos[0].get("url", "").replace("square", "medium") if photos else ""
    sci_name      = taxon.get("name", "")
    wikipedia_url = taxon.get("wikipedia_url", "")

    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_taxonomy = ex.submit(get_taxonomy, taxon["id"])
        fut_wiki     = ex.submit(get_wikipedia_extras, sci_name, wikipedia_url)
        taxonomy     = fut_taxonomy.result()
        extras       = fut_wiki.result()

    phylum  = taxonomy.get("phylum",  "—")
    kingdom = taxonomy.get("kingdom", "—")

    return {
        "id":              taxon.get("id"),
        "common_name":     taxon.get("preferred_common_name") or sci_name or "Unknown",
        "scientific_name": sci_name,
        "kingdom":         kingdom,
        "phylum":          phylum,
        "class_":          taxonomy.get("class",  "—"),
        "order_":          taxonomy.get("order",  "—"),
        "family":          taxonomy.get("family", "—"),
        "genus":           taxonomy.get("genus",  "—"),
        "image_url":       image_url,
        "wikipedia_url":   wikipedia_url,
        "habitat":         extras["habitat"],
        "feeding":         extras["feeding"],
    }
# ── FETCH RANDOM ─────────────────────────────────────────────────────────────
def fetch_random_animal(phyla_filter=None):
    pool = []
    for group in TAXON_GROUPS:
        pool.extend([group] * group["weight"])

    valid_taxon_ids = None
    valid_phyla     = None

    if phyla_filter:
        matched_groups = [
            g for g in TAXON_GROUPS
            if g["name"] in phyla_filter or g["phylum"] in phyla_filter
        ]
        if matched_groups:
            valid_taxon_ids = {g["taxon_id"] for g in matched_groups}
            valid_phyla     = {g["phylum"]   for g in matched_groups}
            pool = [g for g in pool if g["taxon_id"] in valid_taxon_ids]

    pool_copy = pool[:]
    random.shuffle(pool_copy)

    for attempt in range(20):
        group     = random.choice(pool_copy)
        is_plant  = group["phylum"] == "Plantae"
        rand_page = random.randint(1, 5)

        for page in [rand_page] + list(range(1, 6)):
            try:
                params = {
                    "place_id": SINGAPORE_PLACE_ID,
                    "taxon_id": group["taxon_id"],
                    "photos":   "true",
                    "per_page": 20,
                    "page":     page,
                    "order":    "random",
                    "captive":  "false",
                }
                if not is_plant:
                    params["quality_grade"] = "research,needs_id"

                resp = requests.get(
                    "https://api.inaturalist.org/v1/observations",
                    params=params,
                    timeout=6
                )
                resp.raise_for_status()
                results = resp.json().get("results", [])

                if not results:
                    if page == 1:
                        break
                    continue

                random.shuffle(results)
                for obs in results:
                    entry = build_observation(obs)
                    if not entry:
                        continue

                    if valid_phyla:
                        # For plants: phylum is "—", so also check kingdom
                        taxon_phylum  = entry["phylum"]
                        taxon_kingdom = entry["kingdom"]
                        if taxon_phylum not in valid_phyla and taxon_kingdom not in valid_phyla:
                            continue

                    return entry

                break

            except requests.exceptions.Timeout:
                break
            except Exception:
                break

    return None
# ── FETCH LIST (browse mode) ─────────────────────────────────────────────────
def fetch_singapore_animals(query="", page=1, per_page=20, phyla_filter=None):
    params = {
        "place_id":  SINGAPORE_PLACE_ID,
        "taxon_id":  1,
        "photos":    "true",
        "per_page":  per_page,
        "page":      page,
        "order":     "desc",
        "order_by":  "votes",
    }
    if query:
        params["taxon_name"] = query

    resp = requests.get("https://api.inaturalist.org/v1/observations",
                        params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    seen_taxa = set()
    obs_list  = []
    for obs in data.get("results", []):
        taxon = obs.get("taxon")
        if not taxon:
            continue
        tid = taxon.get("id")
        if tid in seen_taxa:
            continue
        seen_taxa.add(tid)
        obs_list.append(obs)

    # Build all observations in parallel
    results = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(build_observation, obs): obs for obs in obs_list}
        for fut in as_completed(futures):
            entry = fut.result()
            if entry:
                results.append(entry)

    if phyla_filter:
        results = [r for r in results if r["phylum"] in phyla_filter]

    return results, data.get("total_results", 0)
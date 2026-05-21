from flask import Flask, render_template, request, jsonify
from scraper import fetch_singapore_animals, fetch_random_animal

app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/animals")
def animals():
    query      = request.args.get("q", "")
    page       = int(request.args.get("page", 1))
    per_page   = int(request.args.get("per_page", 20))
    phyla      = request.args.get("phyla", "")
    phyla_list = [p.strip() for p in phyla.split(",") if p.strip()]
    try:
        results, total = fetch_singapore_animals(query, page, per_page, phyla_list)
        return jsonify({"results": results, "total": total, "page": page})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/random")
def random_animal():
    try:
        phyla      = request.args.get("phyla", "")
        phyla_list = [p.strip() for p in phyla.split(",") if p.strip()]
        animal     = fetch_random_animal(phyla_filter=phyla_list if phyla_list else None)
        if animal:
            return jsonify(animal)
        return jsonify({"error": "No result — try broadening your filter."}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)
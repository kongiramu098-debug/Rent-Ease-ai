"""RentEase AI backend: Flask + MongoDB (PyMongo). Untested prototype."""
import os, secrets
from datetime import date, datetime
from functools import wraps
from bson import ObjectId
from dotenv import load_dotenv
from flask import Flask, jsonify, request, session
from pymongo import MongoClient, ASCENDING
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()
app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.environ["SECRET_KEY"]
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.getenv("HTTPS", "0") == "1")
db = MongoClient(os.environ["MONGO_URI"])[os.getenv("MONGO_DB", "rentease")]

STEPS = ["Booking Pending", "Booking Confirmed", "Payment Completed", "Ready for Pickup",
         "Rental Active", "Return Requested", "Returned", "Completed"]
DONE = {"Cancelled", "Returned", "Completed"}

def out(d):
    d = dict(d); d["id"] = str(d.pop("_id")); d.pop("pw", None)
    for k in ("user_id", "product_id", "booking_id"):
        if k in d: d[k] = str(d[k])
    return d

def current():
    uid = session.get("uid")
    return db.users.find_one({"_id": ObjectId(uid)}) if uid else None

def need(*roles):
    def deco(f):
        @wraps(f)
        def w(*a, **kw):
            u = current()
            if not u: return jsonify(error="Log in first"), 401
            if roles and u["role"] not in roles: return jsonify(error="Not allowed"), 403
            return f(u, *a, **kw)
        return w
    return deco

def parse(d):
    try: return date.fromisoformat(d)
    except Exception: return None

@app.get("/")
def index():
    return app.send_static_file("index.html")

@app.get("/api/me")
def me():
    u = current()
    return (jsonify(id=str(u["_id"]), name=u["name"], role=u["role"]) if u else (jsonify(error="Not logged in"), 401))

@app.get("/api/availability")
def availability():
    a, b = parse(request.args.get("start")), parse(request.args.get("end"))
    try: pid = ObjectId(request.args.get("product_id"))
    except Exception: return jsonify(error="Invalid product"), 400
    p = db.products.find_one({"_id": pid})
    if not p or not a or not b or b < a or a < date.today(): return jsonify(error="Choose valid dates"), 400
    d = (b - a).days + 1
    return jsonify(available=free_units(pid, a, b), days=d, rent=p["price"] * d, deposit=p["dep"])

# ---------- auth ----------
@app.post("/api/register")
def register():
    j = request.get_json(force=True)
    name, email, pw = j.get("name", "").strip(), j.get("email", "").strip().lower(), j.get("password", "")
    role = j.get("role", "customer")
    if not name or "@" not in email or len(pw) < 6: return jsonify(error="Name, valid email and 6+ char password required"), 400
    if role not in ("customer", "owner"): return jsonify(error="Invalid role"), 400
    if db.users.find_one({"email": email}): return jsonify(error="Email already registered"), 409
    r = db.users.insert_one({"name": name, "email": email, "pw": generate_password_hash(pw),
                             "role": role, "approved": False, "created_at": datetime.utcnow()})
    session["uid"] = str(r.inserted_id)
    return jsonify(id=str(r.inserted_id), role=role), 201

@app.post("/api/login")
def login():
    j = request.get_json(force=True)
    u = db.users.find_one({"email": j.get("email", "").strip().lower()})
    if not u or not check_password_hash(u["pw"], j.get("password", "")): return jsonify(error="Email or password is incorrect"), 401
    session["uid"] = str(u["_id"])
    return jsonify(id=str(u["_id"]), role=u["role"])

@app.post("/api/logout")
def logout():
    session.clear(); return jsonify(ok=True)

# ---------- products ----------
@app.get("/api/products")
def products():
    q = {}
    if request.args.get("brand"): q["brand"] = request.args["brand"]
    if request.args.get("category"): q["cat"] = request.args["category"]
    if request.args.get("q"): q["name"] = {"$regex": request.args["q"], "$options": "i"}
    order = ASCENDING if request.args.get("order", "asc") == "asc" else -1
    return jsonify([out(p) for p in db.products.find(q).sort("year", order)])

# ---------- bookings ----------
def free_units(pid, a, b):
    p = db.products.find_one({"_id": pid})
    used = db.bookings.count_documents({"product_id": pid, "status": {"$nin": list(DONE)},
                                        "start": {"$lte": b.isoformat()}, "end": {"$gte": a.isoformat()}})
    return p["qty"] - used

@app.post("/api/bookings")
@need("customer")
def book(u):
    j = request.get_json(force=True)
    a, b = parse(j.get("start")), parse(j.get("end"))
    if not a or not b or b < a or a < date.today(): return jsonify(error="Invalid dates"), 400
    try: pid = ObjectId(j.get("product_id"))
    except Exception: return jsonify(error="Invalid product"), 400
    p = db.products.find_one({"_id": pid})
    if not p: return jsonify(error="Product not found"), 404
    # NOTE: check-then-insert can race. On a replica set/Atlas, wrap in a transaction.
    if free_units(pid, a, b) < 1: return jsonify(error="Not available for those dates"), 409
    days = (b - a).days + 1
    bk = {"user_id": u["_id"], "product_id": pid, "start": a.isoformat(), "end": b.isoformat(),
          "days": days, "rent": p["price"] * days, "deposit": p["dep"], "status": "Booking Pending",
          "paid": False, "log": [["Booking Pending", datetime.utcnow().isoformat()]],
          "created_at": datetime.utcnow()}
    bk["_id"] = db.bookings.insert_one(bk).inserted_id
    return jsonify(out(bk)), 201

@app.get("/api/bookings")
@need()
def my_bookings(u):
    q = {} if u["role"] == "admin" else {"user_id": u["_id"]}
    return jsonify([out(b) for b in db.bookings.find(q).sort("created_at", -1)])

@app.post("/api/bookings/<bid>/cancel")
@need("customer")
def cancel(u, bid):
    b = db.bookings.find_one({"_id": ObjectId(bid), "user_id": u["_id"]})
    if not b: return jsonify(error="Not found"), 404
    if STEPS.index(b["status"]) > 2 if b["status"] in STEPS else True: return jsonify(error="Cannot cancel now"), 409
    db.bookings.update_one({"_id": b["_id"]}, {"$set": {"status": "Cancelled"},
                           "$push": {"log": ["Cancelled", datetime.utcnow().isoformat()]}})
    return jsonify(ok=True)

@app.patch("/api/bookings/<bid>/status")
@need("admin")
def advance(u, bid):
    b = db.bookings.find_one({"_id": ObjectId(bid)})
    if not b or b["status"] not in STEPS or b["status"] == STEPS[-1]: return jsonify(error="Cannot advance"), 409
    nxt = STEPS[STEPS.index(b["status"]) + 1]
    if not b["paid"] and STEPS.index(nxt) >= 2: return jsonify(error="Payment pending"), 409
    db.bookings.update_one({"_id": b["_id"]}, {"$set": {"status": nxt}, "$push": {"log": [nxt, datetime.utcnow().isoformat()]}})
    return jsonify(status=nxt)

# ---------- payments (mock; swap in Razorpay order + signature verification) ----------
@app.post("/api/payments/create")
@need("customer")
def pay(u):
    j = request.get_json(force=True)
    b = db.bookings.find_one({"_id": ObjectId(j.get("booking_id")), "user_id": u["_id"]})
    if not b or b["paid"]: return jsonify(error="Booking not payable"), 409
    amt = b["rent"] + b["deposit"]  # amount always computed server-side
    p = {"booking_id": b["_id"], "user_id": u["_id"], "amount": amt, "method": j.get("method", "UPI"),
         "txn": "pay_" + secrets.token_hex(5), "status": "Paid", "created_at": datetime.utcnow()}
    db.payments.insert_one(p)
    now = datetime.utcnow().isoformat()
    db.bookings.update_one({"_id": b["_id"]}, {"$set": {"paid": True, "status": "Payment Completed"},
                           "$push": {"log": {"$each": [["Booking Confirmed", now], ["Payment Completed", now]]}}})
    return jsonify(txn=p["txn"], amount=amt), 201

# ---------- admin ----------
@app.get("/api/admin/users")
@need("admin")
def users(u):
    return jsonify([out(x) for x in db.users.find()])

@app.post("/api/admin/users")
@need("admin")
def add_user(u):
    j = request.get_json(force=True)
    email = j.get("email", "").strip().lower()
    if j.get("role") not in ("customer", "owner", "admin") or "@" not in email or len(j.get("password", "")) < 6:
        return jsonify(error="Invalid input"), 400
    if db.users.find_one({"email": email}): return jsonify(error="Email already registered"), 409
    r = db.users.insert_one({"name": j.get("name", "").strip(), "email": email, "pw": generate_password_hash(j["password"]),
                             "role": j["role"], "approved": j["role"] == "owner", "created_at": datetime.utcnow()})
    return jsonify(id=str(r.inserted_id)), 201

@app.post("/api/admin/users/<uid>/approve")
@need("admin")
def approve(u, uid):
    db.users.update_one({"_id": ObjectId(uid), "role": "owner"}, {"$set": {"approved": True}})
    return jsonify(ok=True)

@app.delete("/api/admin/users/<uid>")
@need("admin")
def del_user(u, uid):
    if uid == str(u["_id"]): return jsonify(error="Cannot remove yourself"), 400
    if db.bookings.count_documents({"user_id": ObjectId(uid), "status": {"$nin": list(DONE)}}):
        return jsonify(error="User has active bookings"), 409
    db.users.delete_one({"_id": ObjectId(uid)}); return jsonify(ok=True)

@app.get("/api/admin/payments")
@need("admin")
def all_payments(u):
    return jsonify([out(p) for p in db.payments.find().sort("created_at", -1)])

# ---------- seed ----------
def seed():
    db.users.create_index("email", unique=True)
    db.products.create_index([("brand", 1), ("year", 1)])
    db.bookings.create_index([("product_id", 1), ("start", 1), ("end", 1)])
    if not db.users.find_one({"role": "admin"}):
        pw = os.environ["ADMIN_PASSWORD"]
        db.users.insert_one({"name": "Admin", "email": os.getenv("ADMIN_EMAIL", "admin@rentease.com"),
                             "pw": generate_password_hash(pw), "role": "admin", "approved": True, "created_at": datetime.utcnow()})
    if db.products.count_documents({}) == 0:
        L = [("Apple","MacBook Air M1",2020,380,4500,2),("Apple","MacBook Pro 14 M1 Pro",2021,650,9000,1),("Apple","MacBook Air M2",2022,450,5000,2),
             ("Apple","MacBook Air M3",2024,550,6000,2),("Apple","MacBook Pro 14 M4",2024,900,10000,1),
             ("Lenovo","ThinkPad T480",2018,220,2500,3),("Lenovo","IdeaPad Slim 3",2021,260,2500,3),("Lenovo","ThinkPad X1 Carbon Gen 10",2022,500,5000,2),
             ("Lenovo","Legion 5 Pro",2023,600,6000,2),("Lenovo","Yoga Slim 7",2024,480,4500,2),
             ("Asus","VivoBook 15",2020,230,2500,3),("Asus","ROG Zephyrus G14",2021,650,7000,1),("Asus","ZenBook 14 OLED",2022,420,4000,2),
             ("Asus","TUF Gaming A15",2023,480,5000,2),("Asus","ROG Strix G16",2024,750,8000,1),
             ("Dell","Inspiron 15 3000",2019,210,2500,3),("Dell","Latitude 7420",2021,380,4000,2),("Dell","XPS 13",2023,400,4000,3),
             ("Dell","XPS 15",2023,650,7000,1),("Dell","Alienware m16",2024,850,9000,1),
             ("HP","Pavilion 15",2019,210,2500,3),("HP","EliteBook 840 G8",2021,360,4000,2),("HP","Spectre x360",2022,520,5500,2),
             ("HP","Victus 16",2023,450,4500,2),("HP","OMEN 16",2024,700,7500,1)]
        db.products.insert_many([{"brand": b, "name": n, "year": y, "price": p, "dep": d, "qty": q, "cat": "Laptops",
                                  "shop": "Bengaluru Tech Rentals"} for b, n, y, p, d, q in L])

if __name__ == "__main__":
    seed()
    ssl = ("cert.pem", "key.pem") if os.path.exists("cert.pem") else None  # local HTTPS
    app.run(port=int(os.getenv("PORT", 5000)), ssl_context=ssl)

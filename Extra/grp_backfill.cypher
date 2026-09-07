// ============================================================
// MMU — GRP Taxonomy Backfill
// Run in Neo4j Browser at http://127.0.0.1:7474
// Run STEP 0 first, then confirm, then run STEP 1
// ============================================================

// STEP 0 — Dedup first (removes 001/006, 002/007, 003/008, 004/009, 005/010 pairs)
// Keep lowest USE counter copy, transfer CO_RECALLED edges before deleting
// Run this block, check output, then proceed to STEP 1

MATCH (a:Memory), (b:Memory)
WHERE a.address < b.address
  AND [kw IN [(a)-[:HAS_KEYWORD]->(k) | k.term] | kw] =
      [kw IN [(b)-[:HAS_KEYWORD]->(k) | k.term] | kw]
RETURN a.address AS keep, b.address AS remove
ORDER BY keep;

// ---- Confirm the above shows only the 5 duplicate pairs ----
// Then run the actual delete:

// MATCH (a:Memory), (b:Memory)
// WHERE a.address < b.address
//   AND [kw IN [(a)-[:HAS_KEYWORD]->(k) | k.term] | kw] =
//       [kw IN [(b)-[:HAS_KEYWORD]->(k) | k.term] | kw]
// DETACH DELETE b;

// ============================================================
// STEP 1 — GRP Taxonomy rename
// Each SET updates the address string in-place.
// After this runs, execute migrate_to_v2_index.py to rebuild
// the light index from the new addresses.
// ============================================================

// --- 1xx PROJECT ---

// 101 Architecture
MATCH (m:Memory {address: "003.002.003.000,000|0.000.000"})
SET m.address = "003.002.101.000,000|0.000.000";

MATCH (m:Memory {address: "004.003.004.000,999|0.000.000"})
SET m.address = "004.003.101.000,999|0.000.000";

// 102 Technical Stack
MATCH (m:Memory {address: "002.001.002.000,000|0.000.000"})
SET m.address = "002.001.102.000,000|0.000.000";

// 103 Testing
MATCH (m:Memory {address: "011.005.011.004,000|1.000.000"})
SET m.address = "011.005.103.004,000|1.000.000";

// --- 2xx PERSONAL ---

// 201 Identity
MATCH (m:Memory {address: "001.001.001.000,000|0.000.000"})
SET m.address = "001.001.201.000,000|0.000.000";

// 202 Biography
MATCH (m:Memory {address: "012.005.012.004,000|1.000.000"})
SET m.address = "012.005.202.004,000|1.000.000";

MATCH (m:Memory {address: "020.005.020.003,000|0.000.000"})
SET m.address = "020.005.202.003,000|0.000.000";

MATCH (m:Memory {address: "029.005.029.000,000|0.000.000"})
SET m.address = "029.005.202.000,000|0.000.000";

// 203 Relationships
MATCH (m:Memory {address: "021.005.021.003,000|0.000.000"})
SET m.address = "021.005.203.003,000|0.000.000";

// 204 Location
MATCH (m:Memory {address: "014.005.014.010,999|0.000.000"})
SET m.address = "014.005.204.010,999|0.000.000";

// 205 Profession
MATCH (m:Memory {address: "015.005.015.004,000|1.000.000"})
SET m.address = "015.005.205.004,000|1.000.000";

// --- 3xx STANDARDS ---

// 301 Communication Style
MATCH (m:Memory {address: "005.004.005.000,999|1.000.000"})
SET m.address = "005.004.301.000,999|1.000.000";

// --- 4xx PREFERENCES ---

// 404 Aesthetic / Design
MATCH (m:Memory {address: "013.005.013.006,000|1.000.000"})
SET m.address = "013.005.404.006,000|1.000.000";

MATCH (m:Memory {address: "030.005.030.000,000|0.000.000"})
SET m.address = "030.005.404.000,000|0.000.000";

MATCH (m:Memory {address: "031.005.031.000,000|0.000.000"})
SET m.address = "031.005.404.000,000|0.000.000";

// --- 5xx EMOTIONAL ---

// 501 Current State
MATCH (m:Memory {address: "023.005.023.005,000|0.000.000"})
SET m.address = "023.005.501.005,000|0.000.000";

// 503 Faith / Spiritual
MATCH (m:Memory {address: "024.005.024.005,000|0.000.000"})
SET m.address = "024.005.503.005,000|0.000.000";

MATCH (m:Memory {address: "025.005.025.005,000|0.000.000"})
SET m.address = "025.005.503.005,000|0.000.000";

// 504 AI Companionship
MATCH (m:Memory {address: "026.005.026.004,000|0.000.000"})
SET m.address = "026.005.504.004,000|0.000.000";

MATCH (m:Memory {address: "027.005.027.004,000|0.000.000"})
SET m.address = "027.005.504.004,000|0.000.000";

// --- 6xx RESEARCH ---

// 601 Physics / Theory
MATCH (m:Memory {address: "016.005.016.004,000|1.000.000"})
SET m.address = "016.005.601.004,000|1.000.000";

MATCH (m:Memory {address: "017.005.017.004,000|1.000.000"})
SET m.address = "017.005.601.004,000|1.000.000";

// 602 Academic Papers
MATCH (m:Memory {address: "018.005.018.004,000|0.000.000"})
SET m.address = "018.005.602.004,000|0.000.000";

MATCH (m:Memory {address: "019.005.019.008,000|1.000.000"})
SET m.address = "019.005.602.008,000|1.000.000";

// --- 8xx INTERESTS / HOBBIES ---

// 800 General
MATCH (m:Memory {address: "028.005.028.000,000|0.000.000"})
SET m.address = "028.005.800.000,000|0.000.000";

// 801 Gaming
MATCH (m:Memory {address: "022.005.022.003,000|0.000.000"})
SET m.address = "022.005.801.003,000|0.000.000";

// --- VERIFY after running ---
// Should show all addresses with meaningful GRP codes (not sequential)
// MATCH (m:Memory)
// RETURN m.address, m.color,
//        split(m.address, '.')[2] AS grp
// ORDER BY grp, m.address;

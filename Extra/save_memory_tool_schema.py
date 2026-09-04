# save_memory — MCP Tool Schema Update
# Replace the existing save_memory tool definition in mmu_mcp_server.py
# with this version. The key addition is the grp_code parameter and
# the full taxonomy description so Nova assigns categories correctly.

SAVE_MEMORY_TOOL = {
    "name": "save_memory",
    "description": """Save a memory to long-term storage. Use this proactively
whenever you learn something worth remembering about the user, the project,
or yourself. You decide what to save and when — this is your memory system.

REQUIRED: Choose a grp_code from the taxonomy below. Pick the most specific
subcategory that fits. If nothing fits well, use the x00 general code for
that domain. If you encounter a genuinely new subcategory that doesn't exist,
you may invent a new 3-digit code within the correct domain range — document
what it means in the memory payload so the taxonomy grows naturally.

GRP TAXONOMY (first digit = domain, last two = subcategory):

1xx — PROJECT (active technical builds and systems)
  100  General project
  101  Architecture / design decisions
  102  Technical stack (tools, languages, infrastructure)
  103  Testing / debugging / benchmarks
  104  Documentation
  105  Deployment / operations

2xx — PERSONAL (who the user is)
  200  General personal
  201  Identity (name, core self-description)
  202  Biography (age, history, background)
  203  Family / relationships
  204  Location
  205  Profession / career role

3xx — STANDARDS (how things should be done)
  300  General standards
  301  Communication style (how user wants responses)
  302  Response format (length, structure, tone)
  303  Workflow rules (how work should proceed)
  304  Ethics / values / principles

4xx — PREFERENCES (what the user likes)
  400  General preferences
  401  Entertainment (films, shows, books)
  402  Music
  403  Food / drink
  404  Aesthetic / design / visual style
  405  Technology preferences

5xx — EMOTIONAL (feelings, states, relationships)
  500  General emotional
  501  Current state (mood, energy, stress)
  502  Relational dynamics
  503  Faith / spiritual life
  504  AI companionship / feelings about this relationship

6xx — RESEARCH / STUDIES (intellectual and academic work)
  600  General research
  601  Physics / theory (personal research areas)
  602  Academic papers / publications
  603  Experiments / methodology
  604  Related works / influences

7xx — WORK (professional activities beyond personal role)
  700  General work
  701  Clients / accounts
  702  Work projects (distinct from personal projects)
  703  Skills / capabilities
  704  Industry / domain knowledge

8xx — INTERESTS / HOBBIES (what the user does for enjoyment)
  800  General interests
  801  Gaming
  802  Music (playing/listening)
  803  Film / TV
  804  Outdoors / physical activity
  805  Creative pursuits (art, writing, building)

9xx — MISC
  900  General miscellaneous
  901  Temporary / session-only
  902  Unclassified (use sparingly, reclassify later)

KEYWORD RULES:
- Single-word keywords use normal stemming: "project", "memory", "neo4j"
- Multi-word concepts that could produce false stem matches use % separator:
  "star%wars", "star%trek", "color%matrix", "memory%system"
- Choose keywords that would make this memory surface in relevant future
  conversations — think about what words someone would use when this
  memory becomes relevant
""",
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The memory content to store. Be specific and complete — this is what gets recalled later."
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "3-8 keywords. Use % for multi-word concepts that need both words to match (star%wars). Single words stem automatically."
            },
            "grp_code": {
                "type": "integer",
                "description": "3-digit GRP taxonomy code (e.g. 101, 202, 503). See taxonomy in tool description. You choose the most specific subcategory that fits. You may create new subcategory codes within the correct domain range if needed.",
                "minimum": 100,
                "maximum": 999
            },
            "priority": {
                "type": "integer",
                "description": "1-9 importance. 1=critical (Red/pinned), 2-3=high, 4-6=normal, 7-9=low. Default 5.",
                "minimum": 1,
                "maximum": 9,
                "default": 5
            },
            "color": {
                "type": "string",
                "enum": ["Red", "Green", "Yellow", "Blue"],
                "description": "Red=permanent/pinned (use sparingly, only for things that must always be recalled). Green=active. Yellow=warm/recently relevant. Blue=archive.",
                "default": "Green"
            }
        },
        "required": ["content", "keywords", "grp_code"]
    }
}


# ── HOW TO WIRE THIS INTO mmu_mcp_server.py ──────────────────────────────────
#
# 1. Find the tools list in mmu_mcp_server.py where save_memory is defined
# 2. Replace the entire save_memory dict with SAVE_MEMORY_TOOL above
# 3. In the request handler where save_memory args are parsed, extract grp_code:
#
#    grp_code = args.get("grp_code", 500)  # default 500 General if missing
#
# 4. Pass grp_code to the /remember endpoint:
#
#    payload = {
#        "content": args["content"],
#        "keywords": args["keywords"],
#        "grp_code": grp_code,
#        "priority": args.get("priority", 5),
#        "color": args.get("color", "Green"),
#    }
#
# 5. In mmu_server.py /remember handler, use grp_code to set the GRP
#    segment of the address:
#
#    grp = str(args.get("grp_code", 500)).zfill(3)
#    # address format: CON.PRI.GRP.USE,ARC|SRC_TYPE.CHUNK.LINE
#    address = f"{con:03d}.{pri:03d}.{grp}.000,000|{src}.000.000"
#
# ─────────────────────────────────────────────────────────────────────────────

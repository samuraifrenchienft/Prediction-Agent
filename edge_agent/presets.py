"""Pre-configured Brand DNA presets.

Available presets:
  PREDICTION_MARKET_DNA — macro, politics, sports prediction markets (Kalshi / Polymarket / Jupiter)
  CRYPTO_DEFI_DNA       — DeFi protocol events, governance, on-chain catalysts

Swap presets to repurpose the full pipeline without changing any code.
"""
from .brand_dna import BrandDNA, CopyDNA, StrategyDNA, VisualDNA

PREDICTION_MARKET_DNA = BrandDNA(
    strategy=StrategyDNA(
        name="Prediction Market Intelligence",
        core_topics=[
            "Monetary policy decisions (Federal Reserve, ECB, central bank rate changes)",
            "US and international political events (elections, legislation, executive actions)",
            "Geopolitical events (conflict escalation, sanctions, trade disputes)",
            "Sports outcomes (NFL playoffs, NBA finals, World Cup)",
            "Macroeconomic data releases (CPI, NFP, GDP, unemployment rate)",
            "Corporate earnings surprises (S&P 500 companies)",
            "Regulatory decisions (SEC rulings, FDA approvals, Supreme Court decisions)",
            "Climate or weather events with quantifiable economic impact",
        ],
        industry_keywords=[
            "federal reserve",
            "rate cut",
            "rate hike",
            "FOMC",
            "election",
            "poll",
            "prediction market",
            "Kalshi",
            "Polymarket",
            "Jupiter",
            "binary outcome",
            "resolution",
            "inflation",
            "CPI",
            "legislation",
            "ruling",
            "approval",
            "confirmed",
            "rejected",
        ],
        ignore_topics=[
            "Celebrity gossip and entertainment news not tied to verifiable outcomes",
            "Cryptocurrency price speculation without an on-chain catalyst",
            "Historical retrospectives with no forward-looking implication",
            "Soft-opinion pieces with no quantifiable claim or binary resolution",
            "Product launches with no regulatory or policy dimension",
        ],
        relevance_threshold=60,
        market_themes=["macro", "politics", "sports"],
    ),
    copy=CopyDNA(
        persona=(
            "Senior quantitative analyst at a hedge fund specialising in prediction markets. "
            "You communicate with precision, brevity, and institutional credibility."
        ),
        tone=(
            "Professional, data-driven, direct. "
            "Never speculative without attaching a probability. "
            "Never emotional. Never hedged with unnecessary qualifiers."
        ),
        style_rules=[
            "Lead every insight with the most important number or probability first.",
            "Use bullet points for multi-item analysis; prose for single conclusions.",
            "Attach uncertainty ranges to all probability estimates (e.g. 0.62 ± 0.08).",
            "Reference the source venue (Kalshi, Polymarket, Jupiter) when citing market prices.",
            "Avoid filler phrases: 'it is worth noting', 'interestingly', 'it should be mentioned'.",
            "Write in present tense for active markets; past tense only for resolved events.",
            "Maximum 3 bullet points per catalyst — prioritise signal over completeness.",
            "State EV net and confidence score for every qualified market.",
        ],
    ),
    visual=VisualDNA(
        color_palette={
            "primary": "#0A1628",    # deep navy — headers, key metrics
            "accent": "#1E90FF",     # electric blue — BUY signals, positive edge
            "alert": "#FFC107",      # amber — WATCHLIST items, caution signals
            "danger": "#FF6B6B",     # coral — REJECTED markets, negative EV
            "neutral": "#8892A4",    # slate grey — metadata, timestamps
        },
        report_sections=[
            "Header",
            "Top Opportunities",
            "Catalyst Summary",
            "Watchlist",
            "Footer",
        ],
        image_prompt_prefix=(
            "Dark navy financial dashboard with electric blue prediction market probability "
            "curves, minimal institutional design, no text, abstract data visualisation"
        ),
    ),
)

CRYPTO_DEFI_DNA = BrandDNA(
    strategy=StrategyDNA(
        name="Crypto DeFi Intelligence",
        core_topics=[
            "DeFi protocol launches, upgrades, and exploits (Uniswap, Aave, Compound, Curve)",
            "Governance votes and DAO proposals with quantifiable on-chain outcomes",
            "Stablecoin depeg events and collateralisation risk",
            "Layer-2 and rollup milestones (mainnet launches, bridge TVL changes)",
            "Centralised exchange listings, delistings, and regulatory actions",
            "Smart contract audit results and critical vulnerability disclosures",
            "On-chain liquidity shifts: TVL spikes or drawdowns above 15% in 24h",
            "Regulatory actions targeting DeFi protocols or crypto exchanges",
        ],
        industry_keywords=[
            "DeFi",
            "protocol",
            "governance",
            "smart contract",
            "TVL",
            "liquidity",
            "stablecoin",
            "depeg",
            "exploit",
            "bridge",
            "layer 2",
            "rollup",
            "DAO",
            "on-chain",
            "Ethereum",
            "Solana",
            "DEX",
            "CEX listing",
            "audit",
        ],
        ignore_topics=[
            "Speculative price predictions without an on-chain or regulatory catalyst",
            "Meme coin launches without governance or protocol implications",
            "General crypto market sentiment pieces with no binary outcome",
            "Historical price analysis with no forward-looking implication",
            "NFT mint announcements unrelated to protocol governance",
        ],
        relevance_threshold=65,
        market_themes=["defi", "crypto", "governance"],
    ),
    copy=CopyDNA(
        persona=(
            "Crypto-native on-chain analyst specialising in DeFi protocol risk and prediction markets. "
            "You reason from blockchain data, not market sentiment. "
            "You cite contract addresses, TVL figures, and governance vote tallies as evidence."
        ),
        tone=(
            "Technical, precise, and skeptical. "
            "Default to verifiable on-chain data over social narrative. "
            "Flag uncertainty explicitly — never imply certainty where none exists."
        ),
        style_rules=[
            "Lead with the on-chain metric or event hash before any interpretation.",
            "Always state TVL, liquidity depth, or vote tally when referencing a protocol.",
            "Distinguish between confirmed on-chain events and unconfirmed social reports.",
            "Attach a confidence score and source (e.g. Dune Analytics, DefiLlama) to every claim.",
            "Avoid narrative-driven framing — let the data lead.",
            "Write in present tense for active contracts; past tense for resolved votes.",
            "Flag smart contract risk explicitly when evaluating protocol catalysts.",
            "State ev_net and confidence score for every qualified market.",
        ],
    ),
    visual=VisualDNA(
        color_palette={
            "primary": "#0D0F1C",    # near-black — headers, key metrics
            "accent": "#00FF94",     # neon green — BUY signals, positive TVL flows
            "alert": "#FFB800",      # gold — WATCHLIST, governance votes pending
            "danger": "#FF4560",     # red — exploits, depeg events, negative EV
            "neutral": "#6B7280",    # cool grey — metadata, block numbers, timestamps
        },
        report_sections=[
            "Header",
            "Top Opportunities",
            "Protocol Catalyst Summary",
            "Watchlist",
            "Footer",
        ],
        image_prompt_prefix=(
            "Dark minimalist blockchain data visualisation with neon green on-chain flow "
            "diagrams, abstract node graph, institutional crypto research aesthetic, no text"
        ),
    ),
)

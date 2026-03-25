const {
    default: makeWASocket,
    useMultiFileAuthState,
    DisconnectReason,
    fetchLatestBaileysVersion,
    makeCacheableSignalKeyStore,
} = require("@whiskeysockets/baileys");
const { Boom } = require("@hapi/boom");
const express = require("express");
const pino = require("pino");
const qrcode = require("qrcode-terminal");
const axios = require("axios");

const app = express();
app.use(express.json());

let sock = null;
let isConnected = false;
let reconnectDelay = 5000; // Start at 5 seconds, doubles each time up to 60s

const TRIGGER_WORDS = ["get news", "tech news", "latest news", "news"];
const PYTHON_AGENT_URL = "http://localhost:5000/fetch-news";

// --------------------------------------------------------------
//  Start WhatsApp connection
// --------------------------------------------------------------
async function startWhatsApp() {
    const { version } = await fetchLatestBaileysVersion();
    console.log(`[WA] Using Baileys version: ${version.join(".")}`);

    const { state, saveCreds } = await useMultiFileAuthState("auth_info");
    const logger = pino({ level: "silent" });

    sock = makeWASocket({
        version,
        auth: {
            creds: state.creds,
            keys: makeCacheableSignalKeyStore(state.keys, logger),
        },
        logger,
        browser: ["News Agent", "Chrome", "120.0.0"],
        connectTimeoutMs: 60000,
        keepAliveIntervalMs: 25000,     // Ping WhatsApp every 25s to stay alive
        retryRequestDelayMs: 2000,      // Wait 2s before retrying failed requests
        generateHighQualityLinkPreview: false,
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", (update) => {
        const { connection, lastDisconnect, qr } = update;

        // Show QR code when available
        if (qr) {
            console.log("\n[WA] 📱 Scan this QR code in WhatsApp > Linked Devices:\n");
            qrcode.generate(qr, { small: true });
            console.log("\n[WA] Waiting for scan...\n");
        }

        if (connection === "close") {
            isConnected = false;
            const statusCode = new Boom(lastDisconnect?.error)?.output?.statusCode;
            const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

            console.log(`[WA] Connection closed. Status: ${statusCode}.`);

            if (shouldReconnect) {
                // Exponential backoff — waits longer each reconnect to avoid WhatsApp blocks
                console.log(`[WA] Reconnecting in ${reconnectDelay / 1000}s...`);
                setTimeout(async () => {
                    reconnectDelay = Math.min(reconnectDelay * 2, 60000); // Cap at 60s
                    await startWhatsApp();
                }, reconnectDelay);
            } else {
                // Logged out — reset delay and ask user to re-scan
                reconnectDelay = 5000;
                console.log("[WA] ⚠️  Logged out. Delete auth_info folder and restart to re-scan QR.");
            }

        } else if (connection === "open") {
            isConnected = true;
            reconnectDelay = 5000; // Reset delay on successful connection
            console.log("[WA] ✅ WhatsApp connected and stable!");
        }
    });

    // --------------------------------------------------------------
    //  Listen for incoming messages
    // --------------------------------------------------------------
    sock.ev.on("messages.upsert", async ({ messages, type }) => {
        if (type !== "notify") return;

        for (const msg of messages) {
            // Ignore messages sent by the bot itself
            if (msg.key.fromMe) return;

            const senderJid = msg.key.remoteJid;

            // Ignore broadcast/status messages
            if (senderJid === "status@broadcast" || senderJid.endsWith("@g.us")) {
                return;
            }

            const senderPhone = senderJid.replace("@s.whatsapp.net", "");

            // Extract message text safely
            const text = (
                msg.message?.conversation ||
                msg.message?.extendedTextMessage?.text ||
                ""
            ).toLowerCase().trim();

            if (!text) return;

            console.log(`[WA] Message from ${senderPhone}: "${text}"`);

            // Check for trigger words
            const isTriggered = TRIGGER_WORDS.some(word => text.includes(word));
            if (!isTriggered) {
                console.log(`[WA] No trigger word found, ignoring.`);
                return;
            }

            console.log(`[WA] Trigger detected! Calling Python agent...`);

            // Acknowledge immediately so user knows the bot is working
            await sock.sendMessage(senderJid, {
                text: "⏳ Fetching the latest news for you, please wait..."
            });

            // Call Python agent
            try {
                await axios.post(PYTHON_AGENT_URL, {
                    jid: senderPhone,
                    query: text,
                });
            } catch (err) {
                console.error("[WA] Failed to call Python agent:", err.message);
                await sock.sendMessage(senderJid, {
                    text: "❌ Something went wrong. Please try again in a moment."
                });
            }
        }
    });
}

// --------------------------------------------------------------
//  POST /send — Python sends articles through here
// --------------------------------------------------------------
app.post("/send", async (req, res) => {
    const { jid, message } = req.body;

    if (!jid || !message) {
        return res.status(400).json({ error: "jid and message are required" });
    }

    if (!sock || !isConnected) {
        return res.status(503).json({ error: "WhatsApp not connected yet." });
    }

    try {
        await sock.sendMessage(jid, { text: message });
        console.log(`[WA] ✅ Article sent to ${jid}`);
        return res.json({ success: true });
    } catch (err) {
        console.error(`[WA] ❌ Failed to send:`, err.message);
        return res.status(500).json({ error: err.message });
    }
});

// --------------------------------------------------------------
//  GET /health
// --------------------------------------------------------------
app.get("/health", (req, res) => {
    res.json({
        status: "ok",
        whatsapp_connected: isConnected,
        reconnect_delay_ms: reconnectDelay,
    });
});

// --------------------------------------------------------------
//  Start
// --------------------------------------------------------------
const PORT = 3000;
app.listen(PORT, async () => {
    console.log(`[SERVER] 🚀 Baileys server running on http://localhost:${PORT}`);
    console.log(`[SERVER] Starting WhatsApp connection...`);
    await startWhatsApp();
});
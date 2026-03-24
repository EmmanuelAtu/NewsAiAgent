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

const app = express();
app.use(express.json());

let sock = null;
let isConnected = false;

// --------------------------------------------------------------
//  Start WhatsApp connection using Baileys
// --------------------------------------------------------------
async function startWhatsApp() {
    // Fetch the latest Baileys version — required for stable connection
    const { version } = await fetchLatestBaileysVersion();
    console.log(`[WA] Using Baileys version: ${version.join(".")}`);

    const { state, saveCreds } = await useMultiFileAuthState("auth_info");

    const logger = pino({ level: "silent" });

    sock = makeWASocket({
        version,
        auth: {
            creds: state.creds,
            // makeCacheableSignalKeyStore prevents key store errors on reconnect
            keys: makeCacheableSignalKeyStore(state.keys, logger),
        },
        logger,
        // Tells WhatsApp this is a Chrome browser — prevents instant disconnects
        browser: ["News Agent", "Chrome", "120.0.0"],
        generateHighQualityLinkPreview: false,
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", (update) => {
        const { connection, lastDisconnect, qr } = update;

        // Show QR code in terminal when available
        if (qr) {
            console.log("\n[WA] 📱 Scan this QR code in WhatsApp > Linked Devices:\n");
            qrcode.generate(qr, { small: true });
            console.log("\n[WA] Waiting for scan...\n");
        }

        if (connection === "close") {
            isConnected = false;
            const statusCode = new Boom(lastDisconnect?.error)?.output?.statusCode;
            const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

            console.log(`[WA] Connection closed. Status code: ${statusCode}. Reconnecting: ${shouldReconnect}`);

            if (shouldReconnect) {
                setTimeout(startWhatsApp, 3000); // Wait 3s before reconnecting
            } else {
                console.log("[WA] Logged out. Delete the auth_info folder and restart.");
            }

        } else if (connection === "open") {
            isConnected = true;
            console.log("[WA] ✅ WhatsApp connected successfully!");
        }
    });
}

// --------------------------------------------------------------
//  POST /send — Python calls this to send a WhatsApp message
// --------------------------------------------------------------
app.post("/send", async (req, res) => {
    const { phone, message } = req.body;

    if (!phone || !message) {
        return res.status(400).json({ error: "phone and message are required" });
    }

    if (!sock || !isConnected) {
        return res.status(503).json({ error: "WhatsApp not connected yet. Scan the QR code first." });
    }

    try {
        const jid = `${phone}@s.whatsapp.net`;
        await sock.sendMessage(jid, { text: message });
        console.log(`[WA] ✅ Message sent to ${phone}`);
        return res.json({ success: true, phone });
    } catch (err) {
        console.error(`[WA] ❌ Failed to send to ${phone}:`, err.message);
        return res.status(500).json({ error: err.message });
    }
});

// --------------------------------------------------------------
//  GET /health — lets Python check if server is up
// --------------------------------------------------------------
app.get("/health", (req, res) => {
    res.json({
        status: "ok",
        whatsapp_connected: isConnected,
    });
});

// --------------------------------------------------------------
//  Start everything
// --------------------------------------------------------------
const PORT = 3000;

app.listen(PORT, async () => {
    console.log(`[SERVER] 🚀 Baileys server running on http://localhost:${PORT}`);
    console.log("[SERVER] Starting WhatsApp connection...");
    await startWhatsApp();
});

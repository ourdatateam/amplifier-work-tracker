#!/usr/bin/env node
// Run as root inside the live NPM18 guest, with cwd=/app. The supported
// internal model API owns proxy-host writes, audit records, nginx configure,
// and reload behavior. Database queries below are read-only.
import fs from "node:fs/promises";
import path from "node:path";
import { spawnSync } from "node:child_process";
import Access from "/app/lib/access.js";
import db from "/app/db.js";
import proxyHost from "/app/internal/proxy-host.js";

const domain = "beads.ssiops.com";
const backupDir = "/data/backups";
const requiredCertDomains = ["*.ssiops.com", "ssiops.com"];
const advancedConfig = "proxy_set_header Host $host;\nproxy_set_header Authorization $http_authorization;";
const settings = {
	domain_names: [domain],
	forward_scheme: "http",
	forward_host: "192.168.200.160",
	forward_port: 80,
	access_list_id: 0,
	certificate_id: 6,
	ssl_forced: 1,
	http2_support: 1,
	allow_websocket_upgrade: 1,
	enabled: 1,
	advanced_config: advancedConfig,
};

function assert(condition, message) {
	if (!condition) throw new Error(message);
}

function jsonValue(value) {
	if (typeof value !== "string") return value;
	try { return JSON.parse(value); } catch { return value; }
}

function normalizeDomains(value) {
	const domains = jsonValue(value);
	return Array.isArray(domains) ? [...domains].sort() : [];
}

function same(value, expected) {
	return Number(value) === Number(expected);
}

function checkNginx() {
	const result = spawnSync("nginx", ["-t"], { encoding: "utf8" });
	assert(!result.error && result.status === 0, "nginx -t failed for the proxy-host configuration.");
}

function isIdentical(row) {
	return normalizeDomains(row.domain_names).join(",") === domain &&
		row.forward_scheme === settings.forward_scheme &&
		row.forward_host === settings.forward_host && same(row.forward_port, settings.forward_port) &&
		same(row.access_list_id, 0) && same(row.certificate_id, 6) &&
		same(row.ssl_forced, 1) && same(row.http2_support, 1) &&
		same(row.allow_websocket_upgrade, 1) && same(row.enabled, 1) &&
		row.advanced_config === advancedConfig;
}

async function backupBeforeMutation(row) {
	await fs.mkdir(backupDir, { recursive: true, mode: 0o700 });
	await fs.chmod(backupDir, 0o700);
	const stamp = new Date().toISOString().replace(/[-:.TZ]/g, "");
	if (row) {
		const recordPath = path.join(backupDir, `beads-${stamp}-proxy-host-${row.id}.json`);
		await fs.writeFile(recordPath, `${JSON.stringify(row, null, 2)}\n`, { flag: "wx", mode: 0o600 });
		await fs.chmod(recordPath, 0o600);
	}
	const databasePath = path.join(backupDir, `beads-${stamp}-database.sqlite`);
	const python = [
		"import os, sqlite3, sys; os.umask(0o077)",
		"source = sqlite3.connect('file:/data/database.sqlite?mode=ro', uri=True)",
		"target = sqlite3.connect(sys.argv[1])",
		"source.backup(target)",
		"target.close(); source.close(); os.chmod(sys.argv[1], 0o600)",
	].join("; ");
	const result = spawnSync("python3", ["-c", python, databasePath], { encoding: "utf8" });
	assert(!result.error && result.status === 0, "SQLite backup failed before mutation.");
	await fs.chmod(databasePath, 0o600);
}

async function main() {
	assert(process.getuid?.() === 0, "Run as root inside the NPM guest.");
	assert(path.resolve(process.cwd()) === "/app", "Run with cwd=/app inside the NPM guest.");
	const access = new Access(null);
	assert(await access.load(true), "NPM trusted internal access context unavailable.");
	const knex = db();
	try {
		const certificate = await knex("certificate").select("id", "domain_names", "is_deleted").where({ id: 6 }).first();
		assert(certificate && !certificate.is_deleted, "NPM certificate id 6 is unavailable.");
		const certDomains = normalizeDomains(certificate.domain_names).sort();
		assert(JSON.stringify(certDomains) === JSON.stringify(requiredCertDomains), "Certificate id 6 domains do not match the reviewed wildcard certificate.");
		const rows = await knex("proxy_host").select("*").where({ is_deleted: 0 });
		const matches = rows.filter((row) => normalizeDomains(row.domain_names).includes(domain));
		assert(matches.length <= 1, "Multiple proxy-host records contain beads.ssiops.com; refusing ambiguous mutation.");
		let row = matches[0];
		if (row && normalizeDomains(row.domain_names).length !== 1) {
			throw new Error("Existing record shares beads.ssiops.com with other domains; refusing to alter them.");
		}
		if (row && isIdentical(row)) {
			const current = await proxyHost.get(access, { id: row.id });
			assert(current.meta?.nginx_online === true, "Existing identical host is not reported nginx_online.");
			checkNginx();
			console.log(`Existing proxy host ${row.id} is identical and nginx_online; no mutation made.`);
			return;
		}
		await backupBeforeMutation(row);
		if (row) {
			await proxyHost.update(access, { id: row.id, ...settings });
		} else {
			row = await proxyHost.create(access, { ...settings });
		}
		const current = await proxyHost.get(access, { id: row.id });
		assert(normalizeDomains(current.domain_names).join(",") === domain, "Configured proxy host domain readback did not match.");
		assert(current.forward_host === settings.forward_host && same(current.forward_port, 80) && current.forward_scheme === "http", "Configured upstream readback did not match.");
		assert(same(current.certificate_id, 6) && same(current.access_list_id, 0) && same(current.ssl_forced, 1) && same(current.http2_support, 1) && same(current.allow_websocket_upgrade, 1) && same(current.enabled, 1), "Configured TLS/access/enabled readback did not match.");
		assert(current.advanced_config === advancedConfig, "Host and Authorization forwarding configuration did not persist.");
		assert(current.meta?.nginx_online === true, "NPM did not report the configured host nginx_online.");
		checkNginx();
		console.log(`Proxy host ${current.id} configured and verified nginx_online; secrets were not read or changed.`);
	} finally {
		await knex.destroy();
	}
}

main().catch((error) => {
	// Avoid printing arbitrary exception objects, which could carry config data.
	console.error(`npm-proxy: ${error?.message || "operation failed"}`);
	process.exitCode = 1;
});

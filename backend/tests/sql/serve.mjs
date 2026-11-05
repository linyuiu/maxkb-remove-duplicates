// Disposable test databases only; no persisted data or external MaxKB connection.
import { PGlite } from "@electric-sql/pglite";
import { vector } from "@electric-sql/pglite-pgvector";
import { PGLiteSocketServer } from "@electric-sql/pglite-socket";
const servers = [];
for (const port of [15438, 15439]) {
  const db = await PGlite.create({ extensions: { vector } });
  const server = new PGLiteSocketServer({ db, port, host: "127.0.0.1" });
  await server.start();
  servers.push({ server, db });
}
console.log("Disposable PostgreSQL test servers ready");
process.on("SIGINT", async () => {
  for (const { server, db } of servers) {
    await server.stop();
    await db.close();
  }
  process.exit(0);
});

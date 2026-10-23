export default async function teardown() {
  // Windows process-tree termination skips Python finally; request graceful fixture cleanup first.
  try {
    await fetch('http://127.0.0.1:8021/__test/shutdown', {
      method: 'POST', headers: { Authorization: 'Bearer ' + 'a'.repeat(40) },
    });
    for (let attempt = 0; attempt < 100; attempt++) {
      try { await fetch('http://127.0.0.1:8021/health/live'); }
      catch { return; }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error('Browser fixture did not stop gracefully.');
  } catch (error) {
    if (!(error instanceof TypeError)) throw error;
  }
}

// Cloudflare Pages Function : protège tout le dashboard par un mot de passe
// partagé (HTTP Basic Auth). Le navigateur retient le mot de passe, donc les
// membres ne le tapent qu'une fois.
//
// À configurer dans Cloudflare : projet Pages → Settings → Variables and
// Secrets → ajouter SITE_PASSWORD = le mot de passe partagé (en "secret").
// L'identifiant demandé peut être n'importe quoi (seul le mot de passe compte).
//
// Ce fichier est ignoré par GitHub Pages ; il ne s'active que lorsque le site
// est servi via Cloudflare Pages.
export async function onRequest(context) {
  const { request, env, next } = context;

  // Ressources publiques (ex. logo utilisé dans la newsletter) : accessibles
  // sans mot de passe, pour qu'elles s'affichent dans les emails, y compris
  // après un transfert.
  const path = new URL(request.url).pathname;
  if (path === "/email-logo.png" || path.startsWith("/public/")) {
    return next();
  }

  const expected = env.SITE_PASSWORD;

  // Sécurité : si aucun mot de passe n'est configuré, on laisse passer
  // (évite de se verrouiller dehors pendant la mise en place).
  if (!expected) return next();

  const header = request.headers.get("Authorization") || "";
  const [scheme, encoded] = header.split(" ");
  if (scheme === "Basic" && encoded) {
    let decoded = "";
    try {
      decoded = atob(encoded);
    } catch (_) {}
    const password = decoded.slice(decoded.indexOf(":") + 1);
    if (password === expected) return next();
  }

  return new Response("Accès réservé aux membres d'Ambition Campus.", {
    status: 401,
    headers: {
      "WWW-Authenticate": 'Basic realm="Veille Ambition Campus", charset="UTF-8"',
      "Content-Type": "text/plain; charset=UTF-8",
    },
  });
}

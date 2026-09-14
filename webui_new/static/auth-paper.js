/* The HTML contains the final route, so the page also works without animation or JS. */
(function () {
    'use strict';

    const page = document.querySelector('.auth-page');
    const paths = [...document.querySelectorAll('[data-route-from]')];
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
    const compactLayout = window.matchMedia('(max-width: 760px)');
    if (!page || !paths.length) return;

    page.dataset.paperState = 'settled';
    if (reducedMotion.matches || compactLayout.matches
        || document.documentElement.dataset.motion === 'off' || document.hidden) return;

    const numberPattern = /-?\d+(?:\.\d+)?/g;
    const routes = paths.map(path => {
        const finalPath = path.getAttribute('d');
        return {
            path,
            finalPath,
            from: path.dataset.routeFrom.match(numberPattern).map(Number),
            to: finalPath.match(numberPattern).map(Number),
            echo: path.classList.contains('auth-route-echo'),
        };
    });
    if (routes.some(route => route.from.length !== route.to.length)) return;

    let frame = 0;
    let startedAt = null;
    let finished = false;
    const duration = 1900;
    const motionObserver = new MutationObserver(() => {
        if (document.documentElement.dataset.motion === 'off') finish();
    });

    function finish() {
        if (finished) return;
        finished = true;
        cancelAnimationFrame(frame);
        routes.forEach(({ path, finalPath }) => {
            path.setAttribute('d', finalPath);
            path.style.removeProperty('opacity');
        });
        page.dataset.paperState = 'settled';
        reducedMotion.removeEventListener('change', finish);
        compactLayout.removeEventListener('change', finish);
        document.removeEventListener('visibilitychange', onVisibilityChange);
        motionObserver.disconnect();
    }

    function onVisibilityChange() {
        if (document.hidden) finish();
    }

    function draw(now) {
        if (finished) return;
        if (startedAt === null) startedAt = now;
        const progress = Math.min(1, (now - startedAt) / duration);
        const eased = 1 - Math.pow(1 - progress, 3);
        routes.forEach(({ path, finalPath, from, to, echo }) => {
            let index = 0;
            path.setAttribute('d', finalPath.replace(numberPattern, () => {
                const value = from[index] + (to[index] - from[index]) * eased;
                index += 1;
                return value.toFixed(2);
            }));
            if (echo) path.style.opacity = String(.32 * Math.sin(Math.PI * progress));
        });
        if (progress < 1) frame = requestAnimationFrame(draw);
        else finish();
    }

    page.dataset.paperState = 'unfolding';
    reducedMotion.addEventListener('change', finish);
    compactLayout.addEventListener('change', finish);
    document.addEventListener('visibilitychange', onVisibilityChange);
    motionObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-motion'] });
    frame = requestAnimationFrame(draw);
})();

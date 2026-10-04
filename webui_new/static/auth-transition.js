(function () {
    'use strict';

    let leaving = false;
    window.addEventListener('pageshow', () => {
        leaving = false;
        document.body.classList.remove('auth-leaving');
    });

    document.addEventListener('click', (event) => {
        const link = event.target.closest('a[data-auth-transition]');
        if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || link.target || link.hasAttribute('download')) return;
        const destination = new URL(link.href, window.location.href);
        if (destination.origin !== window.location.origin) return;
        if (window.matchMedia('(prefers-reduced-motion: reduce)').matches || document.documentElement.dataset.motion === 'off') return;
        event.preventDefault();
        if (leaving) return;
        leaving = true;
        document.body.classList.add('auth-leaving');
        window.setTimeout(() => window.location.assign(destination.href), 210);
    });
})();

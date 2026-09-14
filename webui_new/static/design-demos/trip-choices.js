(async function () {
    const status = document.getElementById('previewStatus');
    try {
        const response = await fetch('/static/design-demos/trip-choices-live.json');
        if (!response.ok) throw new Error('尚未生成真实接口快照');
        const data = await response.json();
        window.HommeyTripChoices.configure({ searchPlaces: async () => { throw new Error('预览不调用用户接口'); } });
        document.addEventListener('hommey:submit-message', event => { event.preventDefault(); status.textContent = '这是只读预览。请在实际聊天中选择地点并提交。'; });
        document.getElementById('mount').appendChild(window.HommeyTripChoices.create(data));
    } catch (error) { status.textContent = error.message; }
})();

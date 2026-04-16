let knownRooms = [];

async function fetchRegistry() {
    try {
        // 1. Fetch online nodes
        const res = await fetch("/online_nodes");
        const data = await res.json();
        const things = Array.isArray(data) ? data : [];

        // 2. Fetch active rooms
        const rRes = await fetch("/rooms");
        knownRooms = await rRes.json();

        renderGlobalDirectory(things);
        updateForms(things);
        renderActiveRooms();

        const onlineCount = things.length;
        document.getElementById('nodes-count').innerText = `${onlineCount} Nodes Online`;
    } catch (err) {
        console.error("Failed to fetch registry:", err);
    }
}

function renderGlobalDirectory(things) {
    const list = document.getElementById('global-device-list');
    list.innerHTML = '';

    if (things.length === 0) {
        list.innerHTML = '<li class="empty-state">No devices registered.</li>';
        return;
    }

    things.forEach(item => {
        const t = item.td;
        const li = document.createElement('li');
        li.className = 'device-item';

        let badgeClass = 'status-idle';
        if (item.status === 'busy') badgeClass = 'status-busy';

        const roomLabel = item.room_id ? ` <small>(${item.room_id})</small>` : '';

        li.innerHTML = `
                <div>
                    <strong>${t.id}</strong> <small style="color: #64748b;">[${t["@type"].replace('Node', '')}]</small>${roomLabel}
                </div>
                <span class="status-badge ${badgeClass}">${item.status.toUpperCase()}</span>
            `;
        list.appendChild(li);
    });
}

function updateForms(things) {
    // Collect current selections to preserve them after refresh
    const llmSelect = document.getElementById('llm-select');
    const selectedLlm = llmSelect.value;

    const micGroup = document.getElementById('mic-group');
    const checkedMics = Array.from(micGroup.querySelectorAll('input:checked')).map(el => el.value);

    // Filter idle
    const idleMics = things.filter(item => item.status === 'idle' && item.td["@type"] === 'MicrophoneNode');
    const idleLlms = things.filter(item => item.status === 'idle' && item.td["@type"] === 'LLMNode');

    // Update LLM Select
    llmSelect.innerHTML = '<option value="">-- Select an LLM --</option>';
    if (idleLlms.length === 0) {
        llmSelect.innerHTML = '<option value="">-- No idle LLM nodes --</option>';
    } else {
        idleLlms.forEach(item => {
            const opt = document.createElement('option');
            opt.value = item.td.id;
            opt.textContent = item.td.id;
            if (item.td.id === selectedLlm) opt.selected = true;
            llmSelect.appendChild(opt);
        });
    }

    // Update Mic Checkboxes
    if (idleMics.length === 0) {
        micGroup.innerHTML = '<div class="empty-state">No idle microphones ready.</div>';
    } else {
        micGroup.innerHTML = '';
        idleMics.forEach(item => {
            const div = document.createElement('div');
            div.className = 'checkbox-item';
            const isChecked = checkedMics.includes(item.td.id) ? 'checked' : '';
            div.innerHTML = `
                    <input type="checkbox" id="chk-${item.td.id}" value="${item.td.id}" ${isChecked}>
                    <label for="chk-${item.td.id}" style="margin: 0; font-weight: normal;">${item.td.id}</label>
                `;
            micGroup.appendChild(div);
        });
    }
}

function renderActiveRooms() {
    const container = document.getElementById('active-rooms-container');
    if (!knownRooms || knownRooms.length === 0) {
        container.innerHTML = '<div class="empty-state">No rooms are currently provisioned.</div>';
        return;
    }

    container.innerHTML = '';
    knownRooms.forEach(room => {
        const card = document.createElement('div');
        card.className = 'room-card';
        card.innerHTML = `
                <h3>
                    ${room.room_id}
                    <button class="btn-danger" onclick="deleteRoom('${room.room_id}')">Disband Room</button>
                </h3>
                <p><strong>Participants:</strong> ${room.participants.join(', ')}</p>
            `;
        container.appendChild(card);
    });
}

document.getElementById('assign-form').addEventListener('submit', async (e) => {
    e.preventDefault();

    const roomId = document.getElementById('room-id').value.trim();
    const llm = document.getElementById('llm-select').value;
    const mics = Array.from(document.querySelectorAll('#mic-group input:checked')).map(el => el.value);

    if (!roomId || !llm || mics.length === 0) {
        alert("Please provide a Room ID, select an LLM, and select at least one Microphone.");
        return;
    }

    const payload = {
        room_id: roomId,
        llm: llm,
        mics: mics
    };

    const btn = document.getElementById('btn-submit');
    btn.disabled = true;
    btn.innerText = 'Provisioning...';

    try {
        const res = await fetch("/rooms/create", {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (res.ok) {
            document.getElementById('room-id').value = '';
            alert(`Room ${roomId} provisioned successfully!`);
            fetchRegistry(); // refresh instantly
        } else {
            alert("Failed to provision room.");
        }
    } catch (err) {
        console.error(err);
        alert("Network error.");
    } finally {
        btn.disabled = false;
        btn.innerText = 'Provision Room';
    }
});

async function deleteRoom(roomId) {
    if (!confirm(`Are you sure you want to disband ${roomId}? All nodes will become idle.`)) return;

    try {
        const res = await fetch(`/rooms/${roomId}`, { method: 'DELETE' });
        if (res.ok) {
            fetchRegistry();
        } else {
            alert('Failed to delete room.');
        }
    } catch (err) {
        console.error(err);
    }
}

// Poll every 3 seconds
setInterval(fetchRegistry, 3000);
// Initial fetch
fetchRegistry();
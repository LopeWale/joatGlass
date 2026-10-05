import type { Places, SavedPlace } from './Places';
import { PLACE_ADDRESS_MAX_CHARS, PLACE_NAME_MAX_CHARS } from './Places';

interface PlacesPanelOptions {
  places: Places;
  /** The phone's current fix, for "Use my location". */
  currentLocation: () => Promise<{ latitude: number; longitude: number; accuracy?: number } | null>;
}

/**
 * The Places tab of the phone page: the wearer's saved places (Home, Work,
 * ...), each a name plus an address and/or the phone's coordinates. The
 * page is the editor; the glasses list the places on the Navigation page
 * and "navigate home" resolves on the Mac. No HUD ownership here.
 */
export class PlacesPanel {
  private editing?: string;
  private readonly list = document.querySelector<HTMLUListElement>('#p-places');
  private readonly form = document.querySelector<HTMLFormElement>('#p-place-form');
  private readonly name = document.querySelector<HTMLInputElement>('#p-place-name');
  private readonly address = document.querySelector<HTMLInputElement>('#p-place-address');
  private readonly coords = document.querySelector<HTMLElement>('#p-place-coords');
  private readonly locate = document.querySelector<HTMLButtonElement>('#p-place-locate');
  private readonly cancel = document.querySelector<HTMLButtonElement>('#p-place-cancel');
  private readonly add = document.querySelector<HTMLButtonElement>('#p-place-add');
  private readonly feedback = document.querySelector<HTMLElement>('#p-place-feedback');
  private location?: { latitude: number; longitude: number };

  constructor(private readonly options: PlacesPanelOptions) {
    if (this.name) this.name.maxLength = PLACE_NAME_MAX_CHARS;
    if (this.address) this.address.maxLength = PLACE_ADDRESS_MAX_CHARS;
    this.add?.addEventListener('click', () => this.openForm());
    this.cancel?.addEventListener('click', () => this.closeForm());
    this.locate?.addEventListener('click', () => { void this.useLocation(); });
    this.form?.addEventListener('submit', (event) => {
      event.preventDefault();
      this.submit();
    });
    this.list?.addEventListener('click', (event) => {
      const button = (event.target as HTMLElement | null)?.closest<HTMLButtonElement>('button[data-place]');
      if (!button) return;
      const id = button.dataset.place ?? '';
      if (button.dataset.action === 'delete') {
        if (this.options.places.remove(id)) this.say('Place removed.');
      } else {
        const place = this.options.places.list().find((candidate) => candidate.id === id);
        if (place) this.openForm(place);
      }
    });
    options.places.onChange(() => this.render());
    this.render();
  }

  render(): void {
    if (!this.list) return;
    const places = this.options.places.list();
    this.list.replaceChildren(...places.map((place) => this.row(place)));
    const empty = document.getElementById('p-places-empty');
    if (empty) empty.hidden = places.length > 0;
  }

  private row(place: SavedPlace): HTMLLIElement {
    const item = document.createElement('li');
    item.className = 'row place';
    const text = document.createElement('div');
    text.className = 'place-text';
    const title = document.createElement('strong');
    title.textContent = place.name;
    const detail = document.createElement('span');
    detail.textContent = place.address
      ?? (place.latitude !== undefined && place.longitude !== undefined
        ? `${place.latitude.toFixed(5)}, ${place.longitude.toFixed(5)}`
        : '');
    text.append(title, detail);
    const edit = document.createElement('button');
    edit.type = 'button';
    edit.dataset.place = place.id;
    edit.dataset.action = 'edit';
    edit.textContent = 'Edit';
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.dataset.place = place.id;
    remove.dataset.action = 'delete';
    remove.textContent = 'Delete';
    remove.setAttribute('aria-label', `Delete ${place.name}`);
    item.append(text, edit, remove);
    return item;
  }

  private openForm(place?: SavedPlace): void {
    this.editing = place?.id;
    this.location = place && place.latitude !== undefined && place.longitude !== undefined
      ? { latitude: place.latitude, longitude: place.longitude }
      : undefined;
    if (this.name) this.name.value = place?.name ?? '';
    if (this.address) this.address.value = place?.address ?? '';
    this.showCoords();
    if (this.form) this.form.hidden = false;
    if (this.add) this.add.hidden = true;
    this.say('');
    this.name?.focus();
  }

  private closeForm(): void {
    this.editing = undefined;
    this.location = undefined;
    if (this.form) this.form.hidden = true;
    if (this.add) this.add.hidden = false;
  }

  private submit(): void {
    const input = {
      name: this.name?.value ?? '',
      address: this.address?.value ?? '',
      ...(this.location ?? {}),
    };
    if (!input.name.trim()) {
      this.say('Give the place a name, like Home or Work.');
      return;
    }
    if (!input.address.trim() && !this.location) {
      this.say('Add an address, or use your location.');
      return;
    }
    const saved = this.editing
      ? this.options.places.update(this.editing, input)
      : this.options.places.add(input);
    if (!saved) {
      this.say('That place could not be saved.');
      return;
    }
    this.closeForm();
    this.say(`${saved.name} saved. It is on the glasses’ Navigation page now.`);
  }

  private async useLocation(): Promise<void> {
    if (!this.locate || this.locate.disabled) return;
    this.locate.disabled = true;
    this.say('Reading the phone’s location…');
    try {
      const fix = await this.options.currentLocation();
      if (!fix) {
        this.say('No location fix. Allow Location for Even Hub and try again outdoors.');
        return;
      }
      this.location = { latitude: fix.latitude, longitude: fix.longitude };
      this.showCoords();
      this.say(Number.isFinite(fix.accuracy)
        ? `Location captured · accuracy ${Math.round(fix.accuracy!)} m.`
        : 'Location captured.');
    } catch {
      this.say('The location check could not complete.');
    } finally {
      this.locate.disabled = false;
    }
  }

  private showCoords(): void {
    if (!this.coords) return;
    this.coords.textContent = this.location
      ? `Pinned at ${this.location.latitude.toFixed(5)}, ${this.location.longitude.toFixed(5)}`
      : 'No pin yet.';
  }

  private say(text: string): void {
    if (this.feedback) this.feedback.textContent = text;
  }
}

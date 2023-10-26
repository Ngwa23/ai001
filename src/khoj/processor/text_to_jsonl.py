# Standard Packages
from abc import ABC, abstractmethod
import hashlib
import logging
import uuid
from tqdm import tqdm
from itertools import zip_longest
from typing import Callable, List, Tuple, Set, Any
from khoj.utils.helpers import timer


# Internal Packages
from khoj.utils.rawconfig import Entry
from khoj.processor.embeddings import EmbeddingsModel
from khoj.search_filter.date_filter import DateFilter
from database.models import KhojUser, Embeddings, EmbeddingsDates
from database.adapters import EmbeddingsAdapters


logger = logging.getLogger(__name__)


class TextEmbeddings(ABC):
    def __init__(self, config: Any = None):
        self.embeddings_model = EmbeddingsModel()
        self.config = config
        self.date_filter = DateFilter()

    @abstractmethod
    def process(
        self, files: dict[str, str] = None, full_corpus: bool = True, user: KhojUser = None, regenerate: bool = False
    ) -> Tuple[int, int]:
        ...

    @staticmethod
    def hash_func(key: str) -> Callable:
        return lambda entry: hashlib.md5(bytes(getattr(entry, key), encoding="utf-8")).hexdigest()

    @staticmethod
    def split_entries_by_max_tokens(
        entries: List[Entry], max_tokens: int = 256, max_word_length: int = 500
    ) -> List[Entry]:
        "Split entries if compiled entry length exceeds the max tokens supported by the ML model."
        chunked_entries: List[Entry] = []
        for entry in entries:
            # Split entry into words
            compiled_entry_words = [word for word in entry.compiled.split(" ") if word != ""]

            # Drop long words instead of having entry truncated to maintain quality of entry processed by models
            compiled_entry_words = [word for word in compiled_entry_words if len(word) <= max_word_length]
            corpus_id = uuid.uuid4()

            # Split entry into chunks of max tokens
            for chunk_index in range(0, len(compiled_entry_words), max_tokens):
                compiled_entry_words_chunk = compiled_entry_words[chunk_index : chunk_index + max_tokens]
                compiled_entry_chunk = " ".join(compiled_entry_words_chunk)

                # Prepend heading to all other chunks, the first chunk already has heading from original entry
                if chunk_index > 0:
                    # Snip heading to avoid crossing max_tokens limit
                    # Keep last 100 characters of heading as entry heading more important than filename
                    snipped_heading = entry.heading[-100:]
                    compiled_entry_chunk = f"{snipped_heading}.\n{compiled_entry_chunk}"

                chunked_entries.append(
                    Entry(
                        compiled=compiled_entry_chunk,
                        raw=entry.raw,
                        heading=entry.heading,
                        file=entry.file,
                        corpus_id=corpus_id,
                    )
                )

        return chunked_entries

    def update_embeddings(
        self,
        current_entries: List[Entry],
        file_type: str,
        key="compiled",
        logger: logging.Logger = None,
        deletion_filenames: Set[str] = None,
        user: KhojUser = None,
        regenerate: bool = False,
    ):
        # Define the grouper function to split the list into batches
        def grouper(iterable, max_n, fillvalue=None):
            "Split an iterable into chunks of size max_n"
            args = [iter(iterable)] * max_n
            for chunk in zip_longest(*args, fillvalue=fillvalue):
                yield list(filter(lambda x: x is not None, chunk))

        with timer("Construct current entry hashes", logger):
            hashes_by_file = dict[str, set[str]]()
            current_entry_hashes = list(map(TextEmbeddings.hash_func(key), current_entries))
            hash_to_current_entries = dict(zip(current_entry_hashes, current_entries))
            for entry in tqdm(current_entries, desc="Hashing Entries"):
                hashes_by_file.setdefault(entry.file, set()).add(TextEmbeddings.hash_func(key)(entry))

        num_deleted_embeddings = 0
        with timer("Preparing dataset for regeneration", logger):
            if regenerate:
                logger.debug(f"Deleting all embeddings for file type {file_type}")
                num_deleted_embeddings = EmbeddingsAdapters.delete_all_embeddings(user, file_type)

        num_new_embeddings = 0
        with timer("Identify hashes for adding new entries", logger):
            for file in tqdm(hashes_by_file, desc="Processing file with hashed values"):
                hashes_for_file = hashes_by_file[file]
                hashes_to_process = set()
                existing_entries = Embeddings.objects.filter(
                    user=user, hashed_value__in=hashes_for_file, file_type=file_type
                )
                existing_entry_hashes = set([entry.hashed_value for entry in existing_entries])
                hashes_to_process = hashes_for_file - existing_entry_hashes

                entries_to_process = [hash_to_current_entries[hashed_val] for hashed_val in hashes_to_process]
                data_to_embed = [getattr(entry, key) for entry in entries_to_process]
                embeddings = self.embeddings_model.embed_documents(data_to_embed)

                with timer("Update the database with new vector embeddings", logger):
                    num_items = len(hashes_to_process)
                    assert num_items == len(embeddings)
                    zipped_vals = zip(hashes_to_process, embeddings)

                    for batch_zipped_vals in tqdm(
                        grouper(zipped_vals, min(200, num_items)), desc="Processing embeddings in batches"
                    ):
                        batch_embeddings_to_create = []
                        for hashed_val, embedding in batch_zipped_vals:
                            entry = hash_to_current_entries[hashed_val]
                            batch_embeddings_to_create.append(
                                Embeddings(
                                    user=user,
                                    embeddings=embedding,
                                    raw=entry.raw,
                                    compiled=entry.compiled,
                                    heading=entry.heading[:1000],  # Truncate to max chars of field allowed
                                    file_path=entry.file,
                                    file_type=file_type,
                                    hashed_value=hashed_val,
                                    corpus_id=entry.corpus_id,
                                )
                            )
                        new_embeddings = Embeddings.objects.bulk_create(batch_embeddings_to_create)
                        logger.debug(f"Created {len(new_embeddings)} new embeddings")
                        num_new_embeddings += len(new_embeddings)

                        dates_to_create = []
                        with timer("Create new date associations for new embeddings", logger):
                            for embedding in new_embeddings:
                                dates = self.date_filter.extract_dates(embedding.raw)
                                for date in dates:
                                    dates_to_create.append(
                                        EmbeddingsDates(
                                            date=date,
                                            embeddings=embedding,
                                        )
                                    )
                            new_dates = EmbeddingsDates.objects.bulk_create(dates_to_create)
                            if len(new_dates) > 0:
                                logger.debug(f"Created {len(new_dates)} new date entries")

        with timer("Identify hashes for removed entries", logger):
            for file in hashes_by_file:
                existing_entry_hashes = EmbeddingsAdapters.get_existing_entry_hashes_by_file(user, file)
                to_delete_entry_hashes = set(existing_entry_hashes) - hashes_by_file[file]
                num_deleted_embeddings += len(to_delete_entry_hashes)
                EmbeddingsAdapters.delete_embedding_by_hash(user, hashed_values=list(to_delete_entry_hashes))

        with timer("Identify hashes for deleting entries", logger):
            if deletion_filenames is not None:
                for file_path in deletion_filenames:
                    deleted_count = EmbeddingsAdapters.delete_embedding_by_file(user, file_path)
                    num_deleted_embeddings += deleted_count

        return num_new_embeddings, num_deleted_embeddings

    @staticmethod
    def mark_entries_for_update(
        current_entries: List[Entry],
        previous_entries: List[Entry],
        key="compiled",
        logger: logging.Logger = None,
        deletion_filenames: Set[str] = None,
    ):
        # Hash all current and previous entries to identify new entries
        with timer("Hash previous, current entries", logger):
            current_entry_hashes = list(map(TextEmbeddings.hash_func(key), current_entries))
            previous_entry_hashes = list(map(TextEmbeddings.hash_func(key), previous_entries))
            if deletion_filenames is not None:
                deletion_entries = [entry for entry in previous_entries if entry.file in deletion_filenames]
                deletion_entry_hashes = list(map(TextEmbeddings.hash_func(key), deletion_entries))
            else:
                deletion_entry_hashes = []

        with timer("Identify, Mark, Combine new, existing entries", logger):
            hash_to_current_entries = dict(zip(current_entry_hashes, current_entries))
            hash_to_previous_entries = dict(zip(previous_entry_hashes, previous_entries))

            # All entries that did not exist in the previous set are to be added
            new_entry_hashes = set(current_entry_hashes) - set(previous_entry_hashes)
            # All entries that exist in both current and previous sets are kept
            existing_entry_hashes = set(current_entry_hashes) & set(previous_entry_hashes)
            # All entries that exist in the previous set but not in the current set should be preserved
            remaining_entry_hashes = set(previous_entry_hashes) - set(current_entry_hashes)
            # All entries that exist in the previous set and also in the deletions set should be removed
            to_delete_entry_hashes = set(previous_entry_hashes) & set(deletion_entry_hashes)

            preserving_entry_hashes = existing_entry_hashes

            if deletion_filenames is not None:
                preserving_entry_hashes = (
                    (existing_entry_hashes | remaining_entry_hashes)
                    if len(deletion_entry_hashes) == 0
                    else (set(previous_entry_hashes) - to_delete_entry_hashes)
                )

            # load new entries in the order in which they are processed for a stable sort
            new_entries = [
                (current_entry_hashes.index(entry_hash), hash_to_current_entries[entry_hash])
                for entry_hash in new_entry_hashes
            ]
            new_entries_sorted = sorted(new_entries, key=lambda e: e[0])
            # Mark new entries with -1 id to flag for later embeddings generation
            new_entries_sorted = [(-1, entry[1]) for entry in new_entries_sorted]

            # Set id of existing entries to their previous ids to reuse their existing encoded embeddings
            existing_entries = [
                (previous_entry_hashes.index(entry_hash), hash_to_previous_entries[entry_hash])
                for entry_hash in preserving_entry_hashes
            ]
            existing_entries_sorted = sorted(existing_entries, key=lambda e: e[0])

            entries_with_ids = existing_entries_sorted + new_entries_sorted

        return entries_with_ids

    @staticmethod
    def convert_text_maps_to_jsonl(entries: List[Entry]) -> str:
        # Convert each entry to JSON and write to JSONL file
        return "".join([f"{entry.to_json()}\n" for entry in entries])

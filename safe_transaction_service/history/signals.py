from logging import getLogger
from typing import List, Optional, Type, Union

from django.db.models import Model
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from eth_typing import ChecksumAddress

from safe_transaction_service.notifications.tasks import send_notification_task

from ..events.services.queue_service import get_queue_service
from .models import (
    InternalTx,
    MultisigConfirmation,
    MultisigTransaction,
    SafeContract,
    SafeLastStatus,
    SafeMasterCopy,
    SafeStatus,
    TokenTransfer,
)
from .services.notification_service import build_event_payload, is_relevant_notification

logger = getLogger(__name__)


@receiver(
    post_save,
    sender=MultisigConfirmation,
    dispatch_uid="multisig_confirmation.bind_confirmation",
)
@receiver(
    post_save,
    sender=MultisigTransaction,
    dispatch_uid="multisig_transaction.bind_confirmation",
)
def bind_confirmation(
    sender: Type[Model],
    instance: Union[MultisigConfirmation, MultisigTransaction],
    created: bool,
    **kwargs,
) -> None:
    """
    When a `MultisigConfirmation` is saved, it tries to bind it to an existing `MultisigTransaction`, and the opposite.

    :param sender: Could be MultisigConfirmation or MultisigTransaction
    :param instance: Instance of MultisigConfirmation or `MultisigTransaction`
    :param created: `True` if model has just been created, `False` otherwise
    :param kwargs:
    :return:
    """
    if not created:
        return None

    if sender == MultisigTransaction:
        updated = (
            MultisigConfirmation.objects.without_transaction()
            .filter(multisig_transaction_hash=instance.safe_tx_hash)
            .update(multisig_transaction=instance)
        )
        if updated:
            # Update modified on MultisigTransaction if at least one confirmation is added. Tx will now be trusted
            instance.modified = timezone.now()
            instance.trusted = True
            instance.save(update_fields=["modified", "trusted"])
    elif sender == MultisigConfirmation:
        if instance.multisig_transaction_id:
            # Update modified on MultisigTransaction if one confirmation is added. Tx will now be trusted
            MultisigTransaction.objects.filter(
                safe_tx_hash=instance.multisig_transaction_hash
            ).update(modified=instance.created, trusted=True)
        else:
            try:
                if instance.multisig_transaction_hash:
                    multisig_transaction = MultisigTransaction.objects.get(
                        safe_tx_hash=instance.multisig_transaction_hash
                    )
                    multisig_transaction.modified = instance.created
                    multisig_transaction.trusted = True
                    multisig_transaction.save(update_fields=["modified", "trusted"])

                    instance.multisig_transaction = multisig_transaction
                    instance.save(update_fields=["multisig_transaction"])
            except MultisigTransaction.DoesNotExist:
                pass


@receiver(
    post_save,
    sender=SafeMasterCopy,
    dispatch_uid="safe_master_copy.clear_version_cache",
)
def safe_master_copy_clear_cache(
    sender: Type[Model],
    instance: Union[MultisigConfirmation, MultisigTransaction],
    created: bool,
    **kwargs,
) -> None:
    """
    Clear SafeMasterCopy cache if something is modified

    :param sender:
    :param instance:
    :param created:
    :param kwargs:
    :return:
    """
    SafeMasterCopy.objects.get_version_for_address.cache_clear()


def get_safe_addresses_involved_from_db_instance(
    instance: Union[
        TokenTransfer,
        InternalTx,
        MultisigConfirmation,
        MultisigTransaction,
    ]
) -> List[Optional[ChecksumAddress]]:
    """
    Retrieves the Safe addresses involved in the provided database instance.

    :param instance:
    :return: List of Safe addresses from the provided instance
    """
    addresses = []
    if isinstance(instance, TokenTransfer):
        addresses.append(instance.to)
        addresses.append(instance._from)
        return addresses
    elif isinstance(instance, MultisigTransaction):
        addresses.append(instance.safe)
        return addresses
    elif isinstance(instance, MultisigConfirmation) and instance.multisig_transaction:
        addresses.append(instance.multisig_transaction.safe)
        return addresses
    elif isinstance(instance, InternalTx):
        addresses.append(instance.to)
        return addresses

    return addresses


def _process_notification_event(
    sender: Type[Model],
    instance: Union[
        TokenTransfer,
        InternalTx,
        MultisigConfirmation,
        MultisigTransaction,
        SafeContract,
    ],
    created: bool,
    deleted: bool,
):
    assert not (
        created and deleted
    ), "An instance cannot be created and deleted at the same time"

    logger.debug("Start building payloads for created=%s object=%s", created, instance)
    payloads = build_event_payload(sender, instance, deleted=deleted)
    logger.debug(
        "End building payloads %s for created=%s object=%s", payloads, created, instance
    )
    for payload in payloads:
        if address := payload.get("address"):
            if is_relevant_notification(sender, instance, created):
                logger.debug(
                    "Triggering send_notification tasks for created=%s object=%s",
                    created,
                    instance,
                )
                send_notification_task.apply_async(
                    args=(address, payload),
                    countdown=5,
                    priority=2,  # Almost lowest priority
                )
                queue_service = get_queue_service()
                queue_service.send_event(payload)
            else:
                logger.debug(
                    "Notification will not be sent for created=%s object=%s",
                    created,
                    instance,
                )


@receiver(
    post_save,
    sender=SafeLastStatus,
    dispatch_uid="safe_last_status.add_to_historical_table",
)
def add_to_historical_table(
    sender: Type[Model],
    instance: SafeLastStatus,
    created: bool,
    **kwargs,
) -> SafeStatus:
    """
    Add every `SafeLastStatus` entry to `SafeStatus` historical table

    :param sender:
    :param instance:
    :param created:
    :param kwargs:
    :return: SafeStatus
    """
    logger.debug(
        "Storing created=%s object=%s on `SafeStatus` table",
        created,
        instance,
    )
    safe_status = SafeStatus.from_status_instance(instance)
    safe_status.save()
    return safe_status

mod profiles;
#[cfg(test)]
mod tests;
mod v3;
mod v5;

use std::collections::HashSet;

use futures::{Stream, StreamExt, future::join, pin_mut};
use ruma::{
	OwnedRoomId, OwnedUserId, RoomId, UserId,
	events::{
		AnyStrippedStateEvent, StateEventType,
		TimelineEventType::{RoomCreate, RoomMember},
		room::member::{MembershipState, RoomMemberEventContent},
	},
	serde::Raw,
};
use tuwunel_core::{
	Error, PduCount, Result, debug_warn, is_equal_to,
	matrix::{Event, pdu::PduEvent},
	utils::{
		IterStream, ReadyExt, TryFutureExtExt, option::OptionExt, result::LogErr,
		stream::BroadbandExt,
	},
};
use tuwunel_service::{Services, rooms::short::ShortStateHash, users::InviteFilter};

pub(crate) use self::{
	v3::{calculate_heroes, sync_events_route},
	v5::sync_events_v5_route,
};

#[derive(Clone, Copy)]
enum TimelineErrors {
	Ignore,
	Propagate,
}

async fn load_timeline(
	services: &Services,
	sender_user: &UserId,
	room_id: &RoomId,
	roomsincecount: PduCount,
	next_batch: Option<PduCount>,
	limit: usize,
) -> Result<(Vec<(PduCount, PduEvent)>, bool, PduCount), Error> {
	load_timeline_with_errors(
		services,
		sender_user,
		room_id,
		roomsincecount,
		next_batch,
		limit,
		TimelineErrors::Ignore,
	)
	.await
}

async fn load_timeline_fallible(
	services: &Services,
	sender_user: &UserId,
	room_id: &RoomId,
	roomsincecount: PduCount,
	next_batch: Option<PduCount>,
	limit: usize,
) -> Result<(Vec<(PduCount, PduEvent)>, bool, PduCount), Error> {
	load_timeline_with_errors(
		services,
		sender_user,
		room_id,
		roomsincecount,
		next_batch,
		limit,
		TimelineErrors::Propagate,
	)
	.await
}

async fn load_timeline_with_errors(
	services: &Services,
	sender_user: &UserId,
	room_id: &RoomId,
	roomsincecount: PduCount,
	next_batch: Option<PduCount>,
	limit: usize,
	errors: TimelineErrors,
) -> Result<(Vec<(PduCount, PduEvent)>, bool, PduCount), Error> {
	let until = next_batch.map(|count| count.saturating_add(1));
	let pdus = services
		.timeline
		.pdus_rev(Some(sender_user), room_id, until);

	// Take the last events for the timeline.
	pin_mut!(pdus);
	let mut timeline_pdus = Vec::new();
	let mut last_timeline_count = PduCount::max();
	let mut first = true;
	let mut limited = false;

	while let Some(pdu) = pdus.next().await {
		let (pducount, pdu) = match pdu {
			| Ok(pdu) => pdu,
			| Err(error) if first || matches!(errors, TimelineErrors::Propagate) => {
				return Err(error);
			},
			| Err(_) => continue,
		};

		if first {
			first = false;
			last_timeline_count = matches!(pducount, PduCount::Normal(_))
				.then_some(pducount)
				.unwrap_or_else(PduCount::max);
		}

		if pducount <= roomsincecount {
			break;
		}

		if timeline_pdus.len() == limit {
			limited = true;
			break;
		}

		timeline_pdus.push((pducount, pdu));
	}

	timeline_pdus.reverse();

	Ok((timeline_pdus, limited, last_timeline_count))
}

/// Returns the backward pagination token for a timeline slice.
///
/// A slice beginning at room creation has nothing before it, so its window's
/// end lets a members query describe the room as received.
/// Backward pagination from that token re-reads the slice once before ending.
fn timeline_prev_batch(
	timeline_pdus: &[(PduCount, PduEvent)],
	window_end: PduCount,
) -> Option<PduCount> {
	timeline_pdus
		.first()
		.map(|(count, pdu)| match pdu.kind() {
			| RoomCreate => window_end,
			| _ => *count,
		})
}

async fn share_encrypted_room(
	services: &Services,
	sender_user: &UserId,
	user_id: &UserId,
	ignore_room: Option<&RoomId>,
) -> bool {
	services
		.state_cache
		.get_shared_rooms(sender_user, user_id)
		.ready_filter(|&room_id| Some(room_id) != ignore_room)
		.map(ToOwned::to_owned)
		.broad_any(async |other_room_id| {
			services
				.state_accessor
				.is_encrypted_room(&other_room_id)
				.await
		})
		.await
}

/// Members of the rooms `sender_user` left within `(since, next_batch]`, the
/// candidates a leave adds to `device_lists.left`. Rooms still joined supply
/// their candidates from membership changes, and a left room is not visited
/// by that path. Callers keep only users [`share_encrypted_room`] no longer
/// finds. An initial sync has no device list to shrink, so it yields none.
fn left_rooms_members<'a>(
	services: &'a Services,
	sender_user: &'a UserId,
	since: u64,
	next_batch: u64,
) -> impl Stream<Item = OwnedUserId> + Send + 'a {
	services
		.state_cache
		.rooms_left(sender_user)
		.ready_filter(move |_| since != 0)
		.map(ToOwned::to_owned)
		.broad_filter_map(move |room_id| {
			left_room_members(services, sender_user, room_id, since, next_batch)
		})
		.map(IterStream::stream)
		.flatten()
}

/// The room's members `sender_user` could have been tracking: those joined
/// at the start of the window and those joined in the state before the
/// user's leave. The first keeps someone who left earlier in the same
/// window; the second keeps someone who joined since. A member who joined
/// and left entirely inside the window was never reported, so is not named.
async fn left_room_members(
	services: &Services,
	sender_user: &UserId,
	room_id: OwnedRoomId,
	since: u64,
	next_batch: u64,
) -> Option<Vec<OwnedUserId>> {
	let left_count = services
		.state_cache
		.get_left_count(&room_id, sender_user)
		.await
		.ok()?;

	if left_count <= since || left_count > next_batch {
		return None;
	}

	// The shortstatehash stored at the leave PDU is the state before it.
	let at_leave = services
		.timeline
		.get_shortstatehash(&room_id, PduCount::Normal(left_count))
		.ok();

	// State as of `since` is the state before the first PDU after it. The
	// leave falls inside the window, so that PDU exists.
	let at_since = services
		.timeline
		.next_shortstatehash(&room_id, PduCount::Normal(since))
		.ok();

	let (at_leave, at_since) = join(at_leave, at_since).await;
	let at_leave = at_leave?;

	let at_leave = joined_members(services, sender_user, at_leave);
	let at_since = at_since.map_async(|at_since| joined_members(services, sender_user, at_since));

	let (mut members, at_since) = join(at_leave, at_since).await;
	members.extend(at_since.into_iter().flatten());

	Some(members.into_iter().collect())
}

async fn joined_members(
	services: &Services,
	sender_user: &UserId,
	shortstatehash: ShortStateHash,
) -> HashSet<OwnedUserId> {
	services
		.state_accessor
		.state_type_pdus(shortstatehash, &StateEventType::RoomMember)
		.ready_filter_map(|pdu| {
			let content: RoomMemberEventContent = pdu.get_content().ok()?;
			let user_id: OwnedUserId = pdu.state_key()?.parse().ok()?;

			(content.membership == MembershipState::Join && user_id != sender_user)
				.then_some(user_id)
		})
		.collect()
		.await
}

/// MSC4155: whether a stored invite may be served to the invitee.
///
/// The verdict is the recipient's own, judged against the sender of the
/// stripped invite membership event. Unreadable invite state takes the same
/// verdict as the sender-less case below, since neither can name a sender.
async fn invite_permitted_room(
	services: &Services,
	user_id: &UserId,
	room_id: &RoomId,
	filter: &InviteFilter,
) -> bool {
	filter.is_permissive()
		|| services
			.state_cache
			.invite_state(user_id, room_id)
			.await
			.map_or_else(
				|error| {
					debug_warn!(%user_id, %room_id, ?error, "invite state is unreadable; skipping the sender rules");
					filter.permits(None)
				},
				|invite_state| invite_permitted(user_id, room_id, filter, &invite_state),
			)
}

/// [`invite_permitted_room`] for a room whose stripped state is in hand.
///
/// Callers walking stored invites already hold the state and take this form,
/// which spares them the load the room-keyed form pays per room. `room_id`
/// names the affected row in the sender-less diagnostic and does not enter the
/// verdict. That diagnostic stays at debug on a release build deliberately,
/// since the condition repeats for every sync an affected user makes.
fn invite_permitted(
	user_id: &UserId,
	room_id: &RoomId,
	filter: &InviteFilter,
	invite_state: &[Raw<AnyStrippedStateEvent>],
) -> bool {
	// Load-bearing: keeps a permissive user off the sender derivation below.
	if filter.is_permissive() {
		return true;
	}

	let sender = invite_sender(user_id, invite_state);

	if sender.is_none() {
		debug_warn!(%user_id, %room_id, "invite state names no sender; skipping the sender rules");
	}

	filter.permits(sender.as_deref())
}

/// The sender of the stripped membership event inviting `user_id`.
///
/// The last matching entry wins. An invite this server recorded after the
/// federation route began sanitising stripped state holds one entry for this
/// cell, our own copy of the signed membership PDU, whose sender the origin
/// check authenticated. An invite stored before that still carries whatever
/// the inviting server sent ahead of our copy, and the array has no ordering
/// semantics in the spec, so reading the last entry is what keeps those
/// answering with the authenticated sender too.
fn invite_sender(
	user_id: &UserId,
	invite_state: &[Raw<AnyStrippedStateEvent>],
) -> Option<OwnedUserId> {
	invite_state
		.iter()
		.rev()
		.filter(|event| {
			event
				.get_field::<&str>("state_key")
				.is_ok_and(|state_key| state_key.is_some_and(is_equal_to!(user_id.as_str())))
		})
		.filter_map(|event| event.deserialize().ok())
		.find_map(|event| match event {
			| AnyStrippedStateEvent::RoomMember(member) if member.state_key == user_id =>
				Some(member.sender),
			| _ => None,
		})
}

/// State sections strip the stored `prev_content`/`prev_sender` pair
/// (Synapse injects the pair on timeline fetches only). The requester's own
/// membership and events duplicated from the returned timeline (MSC4222,
/// full_state) keep it: clients read membership transitions from those
/// copies.
fn strip_prev_state(
	mut pdu: PduEvent,
	sender_user: &UserId,
	in_timeline: impl Fn(&PduEvent) -> bool,
) -> PduEvent {
	let own_membership =
		*pdu.kind() == RoomMember && pdu.state_key() == Some(sender_user.as_str());

	if !own_membership && !in_timeline(&pdu) {
		pdu.remove_prev_state().log_err().ok();
	}

	pdu
}

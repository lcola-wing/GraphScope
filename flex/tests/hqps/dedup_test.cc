#include <climits>
#include <vector>
#include "flex/engines/hqps_db/core/context.h"
#include "flex/engines/hqps_db/core/params.h"
#include "flex/engines/hqps_db/structures/multi_vertex_set/row_vertex_set.h"
#include "flex/storages/rt_mutable_graph/types.h"

#include "flex/engines/hqps_db/core/operator/project.h"
#include "flex/engines/hqps_db/structures/multi_vertex_set/multi_label_vertex_set.h"
#include "flex/engines/hqps_db/structures/multi_vertex_set/row_vertex_set.h"

#include "flex/engines/hqps_db/database/mutable_csr_interface.h"

#include "grape/types.h"

#include "glog/logging.h"

struct VertexSetTest {};
struct EdgeSetTest {};

using offset_t = gs::offset_t;

auto make_sample_context() {}

namespace gs {
void work() {
  using GI = gs::MutableCSRInterface;
  using vertex_id_t = typename GI::vertex_id_t;
  std::vector<vertex_id_t> vids{0};
  auto set_a = gs::MakeDefaultRowVertexSet(std::move(vids), "a");
  gs::Context<decltype(set_a), 0, 0, grape::EmptyType> ctx_a(std::move(set_a));
  VLOG(10) << "Finish construct set a";

  // add multi label vertex_set

  std::vector<vertex_id_t> vids_b0{1, 2, 1};
  std::vector<size_t> off_b0{0, 3};

  auto set_b = gs::MakeDefaultRowVertexSet(std::move(vids_b0), "b");

  auto ctx_2 =
      ctx_a.AddNode<AppendOpt::Persist>(std::move(set_b), std::move(off_b0));
  for (auto iter : ctx_2) {
    VLOG(10) << gs::to_string(iter.GetAllElement());
  }

  std::vector<vertex_id_t> vids_c0{3, 4, 5, 6, 7};
  std::vector<size_t> off_c0{0, 3, 3, 5};

  auto set_c = gs::MakeDefaultRowVertexSet(std::move(vids_c0), "b");
  auto ctx_3 =
      ctx_2.AddNode<AppendOpt::Persist>(std::move(set_c), std::move(off_c0));
  for (auto iter : ctx_3) {
    VLOG(10) << gs::to_string(iter.GetAllElement());
  }

  ctx_3.Dedup<1>();
  VLOG(10) << "after dedup on 1";
  for (auto iter : ctx_3) {
    VLOG(10) << gs::to_string(iter.GetAllElement());
  }

  auto& select_node = ctx_3.template GetMutableNode<1>();
  // dedup inplace, and return the offset_array to old node.
  auto offset_to_old_node = select_node.Dedup();
  // The offset need to be changed.
  ctx_3.template UpdateChildNode<1>(std::move(offset_to_old_node));

  VLOG(10) << "after dedup on itself";
  for (auto iter : ctx_3) {
    VLOG(10) << gs::to_string(iter.GetAllElement());
  }
}

}  // namespace gs

int main() {
  gs::work();
  LOG(INFO) << "Finish context test.";
}

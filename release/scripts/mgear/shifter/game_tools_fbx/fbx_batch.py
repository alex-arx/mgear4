"""
FBX Batch

Is used to handle tasks that used to be performed by the FBX API.
As the FBX API was an extra module that needed to be downloaded and installed,
it was decided that it would be more beneficial to the user to utilise
Maya Batch to perform the same tasks on the exported FBX file.

Process
-------
1. Shift FBX Exporter, exports the "master" fbx. This will be exported directly from
   the active scene, and should not impact the users scene data in any way.
2. Depending on what options/partitions where active, a variaty of batch tasks will be
   initialised, and perform alterations on the master fbx, and then generate a new FBX
   before ending the batch task.

Tasks / Conditions
------------------
- Removing namespaces.
- Clean up any lingering DAG nodes that are not needed.
- Partition Skeleton + Geometry.
- Exports each partition as an FBX.

"""
import os
import traceback
from collections import OrderedDict

import maya.cmds as cmds
import maya.api.OpenMaya as om

from mgear.core import pyFBX as pfbx


def perform_fbx_condition(remove_namespace, scene_clean, master_fbx_path, root_joint, root_geos, skinning=True, blendshapes=True, partitions=True, export_data=None):
    """
    Performs the FBX file conditioning and partition exports.

    This is called by a MayaBatch process.

    [ ] Setup logging to a text file, so the stream can be monitored.
    [ ] Update FBX export settings

    """
    print("--------------------------")
    print(" PERFORM FBX CONDITIONING")
    print(f"  remove namespace:{remove_namespace}")
    print(f"  clean scene:{scene_clean}")
    print(f"  fbx path : {master_fbx_path}")
    print("--------------------------")

    log_file = "logs.txt"

    # Import fbx into scene
    _import_fbx(master_fbx_path)

    # formats the output location from the master fbx path.
    output_dir = os.path.dirname(master_fbx_path)
    fbx_file = os.path.basename(master_fbx_path)
    conditioned_file = fbx_file.split(".")[0] + "_conditioned.ma"
    
    print(f"  Output location: {output_dir}")
    print(f"  FBX file: {fbx_file}")
    print(f"  Conditioned file: {conditioned_file}")

    # Removes all namespaces from any DG or DAG object.
    if remove_namespace:
        print("Removing Namespace..")
        _clean_namespaces()

        # updates root joint name if namespace is found
        root_joint = root_joint.split(":")[-1]
        for i in range(len(root_geos)):
            root_geos[i] = root_geos[i].split(":")[-1]

    if scene_clean:
        print("Cleaning Scene..")
        # Move the root_joint and root_geos to the scene root
        _parent_to_root(root_joint)
        for r_geo in root_geos:
            _parent_to_root(r_geo)

        # Remove all redundant DAG Nodes.
        _cleanup_stale_dag_hierarchies([root_joint] + root_geos)
    
    if not skinning:
        print("Removing Skinning..")
        # Remove skinning from geometry
        _delete_bind_poses()

    if not blendshapes:
        # Remove blendshapes from geometry
        print("Removing Blendshapes..")
        _delete_blendshapes()

    # Exports the conditioned FBX, over the existing master fbx.
    # The master FBX is now in the correct data state.
    print("Exporting FBX...")
    print("    Path: {}".format(master_fbx_path))
    cmds.select( clear=True )
    cmds.select([root_joint] + root_geos)
    pfbx.FBXExport(f=master_fbx_path, s=True)

    if partitions and export_data is not None:
        print("[Partitions]")
        print("   Preparing scene for Partition creation..")
        # Save out conditioned file, as this will be used by other partition processes
        # Conditioned file, is the file that stores the rig which has already had data
        # update for the export process.
        cmds.file(rename=conditioned_file)
        cmds.file(save=True, force=True, type="mayaAscii")

        _export_skeletal_mesh_partitions([root_joint], export_data, conditioned_file)

        # Delete temporary conditioned .ma file
        cmds.file( new=True, force=True)
        if os.path.exists(conditioned_file):
            os.remove(conditioned_file)
        else:
            print("   Cleaned up conditioned file...")
            print("      Deleted - {}".format(conditioned_file))


def _export_skeletal_mesh_partitions(jnt_roots, export_data, scene_path):
    """
    Exports the individual partition hierarchies that have been specified.

    For each Partition, the conditioned .ma file will be loaded and have 
    alterations performed to it.

    """

    print("   Correlating Mesh to joints...")

    file_path = export_data.get("file_path", "")
    file_name = export_data.get("file_name", "")

    partitions = export_data.get("partitions", dict())
    if not partitions:
        cmds.warning("  Partitions not defined!")
        return False

    # Collects all partition data, so it can be more easily accessed in the next stage
    # where mesh and skeleton data is deleted and exported.

    partitions_data = OrderedDict()
    for partition_name, data in partitions.items():

        print("     Partition: {} \t Data: {}".format(partition_name, data))

        # Skip partition if disabled
        enabled = data.get("enabled", False)
        if not enabled:
            continue

        meshes = data.get("skeletal_meshes", None)

        joint_hierarchy = OrderedDict()
        for mesh in meshes:
            # we retrieve all end joints from the influenced joints
            influences = cmds.skinCluster(mesh, query=True, influence=True)

            # Gets hierarchy from the root joint to the influence joints.
            for jnt_root in jnt_roots:
                joint_hierarchy.setdefault(jnt_root, list())

                for inf_jnt in influences:
                    jnt_hierarchy = _get_joint_list(jnt_root, inf_jnt)
                    for hierarchy_jnt in jnt_hierarchy:
                        if hierarchy_jnt not in joint_hierarchy[jnt_root]:
                            joint_hierarchy[jnt_root].append(hierarchy_jnt)

        partitions_data.setdefault(partition_name, dict())

        # the joint chain to export will be the shorter one between the root joint and the influences
        short_hierarchy = None
        for root_jnt, joint_hierarchy in joint_hierarchy.items():
            total_joints = len(joint_hierarchy)
            if total_joints <= 0:
                continue
            if short_hierarchy is None:
                short_hierarchy = joint_hierarchy
                partitions_data[partition_name]["root"] = root_jnt
            elif len(short_hierarchy) > len(joint_hierarchy):
                short_hierarchy = joint_hierarchy
                partitions_data[partition_name]["root"] = root_jnt
        if short_hierarchy is None:
            continue

        # we make sure we update the hierarchy to include all joints between the skeleton root joint and
        # the first joint of the found joint hierarchy
        root_jnt = _get_root_joint(short_hierarchy[0])
        if root_jnt not in short_hierarchy:
            parent_hierarchy = _get_joint_list(root_jnt, short_hierarchy[0])
            short_hierarchy = parent_hierarchy + short_hierarchy
        partitions_data[partition_name]["hierarchy"] = short_hierarchy

    print("   Modifying Hierarchy...")
    # - Loop over each Partition
    # - Load the master .ma file
    # - Perform removal of geometry, that is not relevent to the partition
    # - Perform removal of skeleton, that is not relevent to the partition
    # - Export an fbx
    for partition_name, partition_data in partitions_data.items():
        if not partition_data:
            print("   Partition {} contains no data.".format(partition_name))
            continue

        print("     {}".format(partition_name))
        print("     {}".format(partition_data))

        partition_meshes = partitions.get(partition_name).get("skeletal_meshes")
        partition_joints = partition_data.get("hierarchy", [])
        # Loads the conditioned scene file, to perform partition actions on.
        cmds.file( scene_path, open=True, force=True, save=False)

        # Deletes meshes that are not included in the partition.
        all_meshes = _get_all_mesh_dag_objects()
        for mesh in all_meshes:
            if not mesh in partition_meshes:
                cmds.delete(mesh)

        # Delete joints that are not included in the partition
        all_joints = _get_all_joint_dag_objects()
        for jnt in reversed(all_joints):
            if not jnt in partition_joints:
                cmds.delete(jnt)

        # Exporting fbx
        partition_file_name = file_name + "_" + partition_name + ".fbx"
        export_path = os.path.join(file_path, partition_file_name)

        print(export_path)
        try:
            preset_path = export_data.get("preset_path", None)
            up_axis = export_data.get("up_axis", None)
            fbx_version = export_data.get("fbx_version", None)
            file_type = export_data.get("file_type", "binary").lower()
            # export settings config
            pfbx.FBXResetExport()
            # set configuration
            if preset_path is not None:
                # load FBX export preset file
                pfbx.FBXLoadExportPresetFile(f=preset_path)
            fbx_version_str = None
            if up_axis is not None:
                pfbx.FBXExportUpAxis(up_axis)
            if fbx_version is not None:
                fbx_version_str = "{}00".format(
                    fbx_version.split("/")[0].replace(" ", "")
                    )
                pfbx.FBXExportFileVersion(v=fbx_version_str)
            if file_type == "ascii":
                pfbx.FBXExportInAscii(v=True)

            cmds.select( clear=True )
            cmds.select(partition_joints + partition_meshes)
            pfbx.FBXExport(f=export_path, s=True)
        except Exception:
            cmds.error(
                "Something wrong happened while export Partition {}: {}".format(
                    partition_name,
                    traceback.format_exc()
                )
            )
    return True


def _delete_blendshapes():
    """
    Deletes all blendshape objects in the scene.
    """
    blendshape_mobjs = _find_dg_nodes_by_type(om.MFn.kBlendShape)
    
    dg_mod = om.MDGModifier()
    for mobj in blendshape_mobjs:
        print("   - {}".format(om.MFnDependencyNode(mobj).name()))
        dg_mod.deleteNode(mobj)

    dg_mod.doIt()


def _find_geometry_dag_objects(parent_object_name):
    selection_list = om.MSelectionList()

    try:
        # Add the parent object to the selection list
        selection_list.add(parent_object_name)

        # Get the MDagPath of the parent object
        parent_dag_path = om.MDagPath()
        parent_dag_path = selection_list.getDagPath(0)

        # Iterate through child objects
        child_count = parent_dag_path.childCount()
        geometry_objects = []

        for i in range(child_count):
            child_obj = parent_dag_path.child(i)
            child_dag_node = om.MFnDagNode(child_obj)
            child_dag_path = child_dag_node.getPath()

            # Check if the child is a geometry node
            if (child_dag_path.hasFn(om.MFn.kMesh) or child_dag_path.hasFn(om.MFn.kNurbsSurface)) and child_dag_path.hasFn(om.MFn.kTransform):
                geometry_objects.append(child_dag_path.fullPathName())

            # Recursive call to find geometry objects under the child
            geometry_objects.extend(_find_geometry_dag_objects(child_dag_path.fullPathName()))

        return geometry_objects

    except Exception as e:
        print(f"Error: {e}")
        return []


def _delete_bind_poses():
    """
    Removes all skin clusters and bind poses nodes from the scene.
    """
    bind_poses_mobjs = _find_dg_nodes_by_type(om.MFn.kDagPose)
    skin_cluster = _find_dg_nodes_by_type(om.MFn.kSkinClusterFilter)

    dg_mod = om.MDGModifier()
    for mobj in bind_poses_mobjs + skin_cluster:
        print("   - {}".format(om.MFnDependencyNode(mobj).name()))
        dg_mod.deleteNode(mobj)

    dg_mod.doIt()


def _find_dg_nodes_by_type(node_type):
    """
    returns a list of MObjects, that match the node type
    """
    dagpose_nodes = []

    # Create an iterator to traverse all dependency nodes
    dep_iter = om.MItDependencyNodes()

    while not dep_iter.isDone():
        current_node = dep_iter.thisNode()

        # Check if the node is a DAG Pose node
        if current_node.hasFn(node_type):
            dagpose_nodes.append(current_node)

        dep_iter.next()

    return dagpose_nodes

def _cleanup_stale_dag_hierarchies(ignore_objects):
    """
    Deletes any dag objects that are not geo or skeleton roots, under the scene root.
    """
    IGNORED_OBJECTS = ['|persp', '|top', '|front', '|side']
    obj_names = _get_dag_objects_under_scene_root()
    
    for i_o in IGNORED_OBJECTS:
        obj_names.remove(i_o)
    
    for i_o in ignore_objects:
        pipped_io = "|"+i_o
        try:
            obj_names.remove(pipped_io)
        except:
            print("  skipped {}".format(pipped_io))

    # Delete left over object hierarchies
    dag_mod = om.MDagModifier()

    for name in obj_names:
        temp_sel = om.MSelectionList()
        temp_sel.add(name)

        if temp_sel.length() != 1:
                continue

        dag_path = temp_sel.getDagPath(0)
        dag_node = om.MFnDagNode(dag_path)
        dag_mod.deleteNode(dag_node.object())
        dag_mod.doIt()


def _parent_to_root(name):
    """
    As Maya's parent command can cause failures if you try and parent the object to 
    the same object it is already parented to. We check the parent, and only if it
    it not the world do we reparent the object.
    """
    temp_sel = om.MSelectionList()
    temp_sel.add(name)

    if temp_sel.length() != 1:
            return

    dag_path = temp_sel.getDagPath(0)
    dag_node = om.MFnDagNode(dag_path)
    parent_obj = dag_node.parent(0)
    parent_name = om.MFnDependencyNode(parent_obj).name()

    if parent_name == "world":
        return

    cmds.parent( name, world=True )

    temp_sel.clear()
    print("  Moved {} to scene root.".format(name))


def _get_dag_objects_under_scene_root():
    """
    Gets a list of all dag objects that direct children of the scene root.
    """
    # Create an MItDag iterator starting from the root of the scene
    dag_iter = om.MItDag(om.MItDag.kDepthFirst, om.MFn.kInvalid)

    dag_objects = []

    while not dag_iter.isDone():
        current_dag_path = dag_iter.getPath()

        # Check if the current DAG path is under the scene root
        if current_dag_path.length() == 1:
            dag_objects.append(current_dag_path.fullPathName())

        # Move to the next DAG object
        dag_iter.next()

    return dag_objects


def _clean_namespaces():
    """
    Gets all available namespaces in scene.
    Checks each for objects that have it assigned.
    Removes the namespace from the object.
    """
    namespaces = _get_scene_namespaces()
    for namespace in namespaces:
        print("  - {}".format(namespace))
        child_namespaces = om.MNamespace.getNamespaces(namespace, True)

        for chld_ns in child_namespaces:
            m_objs = om.MNamespace.getNamespaceObjects(chld_ns)
            for m_obj in m_objs:
                _remove_namespace(m_obj)

        m_objs = om.MNamespace.getNamespaceObjects(namespace)
        for m_obj in m_objs:
            _remove_namespace(m_obj)


def _remove_namespace(mobj):
    """
    Removes the namesspace that is currently assigned to the asset
    """
    dg = om.MFnDependencyNode(mobj)
    name = dg.name()
    dg.setName(name[len(dg.namespace):])


def _get_scene_namespaces():
    """
    Gets all namespaces in the scene.
    """
    IGNORED_NAMESPACES = [":UI", ":shared", ":root"]
    spaces = om.MNamespace.getNamespaces()
    for ignored in IGNORED_NAMESPACES:
        if ignored in spaces:
            spaces.remove(ignored)
    return spaces 


def _import_fbx(file_path):
    try:
        # Import FBX file
        name = cmds.file(file_path, i=True, type="FBX", ignoreVersion=True, ra=True, mergeNamespacesOnClash=False, namespace=":")

        print("FBX file imported successfully.")
        return name

    except Exception as e:
        print("Error importing FBX file:", e)
        return


def _get_joint_list(start_joint, end_joint):
    """Returns a list of joints between and including given start and end joint

    Args:
            start_joint str: start joint of joint list
            end_joint str end joint of joint list

    Returns:
            list[str]: joint list
    """

    sel_list = om.MSelectionList()

    # Tries to convert the start_joint into the full path
    try:
        sel_list.add(start_joint)
        dag_path = sel_list.getDagPath(0)
        full_path = dag_path.fullPathName()
        if start_joint != full_path:
            start_joint = full_path
        sel_list.clear()
    except:
        print("[Error] Start joint {}, could not be found".format(start_joint))
        return []

    # Tries to convert the end_joint into the full path
    try:
        sel_list.add(end_joint)
        dag_path = sel_list.getDagPath(0)
        full_end_joint = dag_path.fullPathName()
        if end_joint != full_end_joint:
            end_joint = full_end_joint
    except:
        print("[Error] End joint {}, could not be found".format(end_joint))
        return []

    if start_joint == end_joint:
        return [start_joint]

    # check hierarchy
    descendant_list = cmds.ls(
        cmds.listRelatives(start_joint, ad=True, fullPath=True),
        long=True,
        type="joint",
    )

    # if the end joint does not exist in the hierarch as the start joint, return
    if not descendant_list.count(end_joint):
        return list()

    joint_list = [end_joint]

    while joint_list[-1] != start_joint:
        parent_jnt = cmds.listRelatives(joint_list[-1], p=True, pa=True, fullPath=True)
        if not parent_jnt:
            raise Exception(
                'Found root joint while searching for start joint "{}"'.format(
                    start_joint
                )
            )
        joint_list.append(parent_jnt[0])

    joint_list.reverse()

    return joint_list


def _get_root_joint(start_joint):
    """
    Recursively traverses up the hierarchy until finding the first object that does not have a parent.

    :param str node_name: node name to get root of.
    :param str node_type: node type for the root node.
    :return: found root node.
    :rtype: str
    """

    parent = cmds.listRelatives(start_joint, parent=True, type="joint")
    parent = parent[0] if parent else None

    return _get_root_joint(parent) if parent else start_joint


def _get_all_mesh_dag_objects():
    """
    Gets all mesh dag objects in scene.

    Only returns DAG object and not the shape node.
    """
    mesh_objects = []

    dag_iter = om.MItDag(om.MItDag.kBreadthFirst)

    while not dag_iter.isDone():
        current_dag_path = dag_iter.getPath()

        # Check if the current object has a mesh function set
        if current_dag_path.hasFn(om.MFn.kMesh):
            if current_dag_path.hasFn(om.MFn.kTransform):
                mesh_objects.append(current_dag_path.fullPathName())

        dag_iter.next()

    return mesh_objects


def _get_all_joint_dag_objects():
    """
    Gets all mesh dag objects in scene.

    Only returns DAG object and not the shape node.
    """
    mesh_objects = []

    dag_iter = om.MItDag(om.MItDag.kBreadthFirst)

    while not dag_iter.isDone():
        current_dag_path = dag_iter.getPath()

        # Check if the current object has a mesh function set
        if current_dag_path.hasFn(om.MFn.kJoint):
            if current_dag_path.hasFn(om.MFn.kTransform):
                mesh_objects.append(current_dag_path.fullPathName())

        dag_iter.next()

    return mesh_objects